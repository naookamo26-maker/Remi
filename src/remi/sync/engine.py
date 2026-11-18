"""1台のサーバーの同期処理(設計書 §4)。

スレッドやイベントに依存しない、同期的な処理だけをここに置く(テストしやすくするため)。
スケジュールとスレッドは remi.sync.worker が受け持つ。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from remi.api.client import RedmineClient
from remi.api.errors import ErrorCode, RedmineError
from remi.store.db import Database
from remi.store.issues import REL_ASSIGNED, REL_AUTHORED, REL_GROUP, REL_WATCHING, IssueStore
from remi.store.masters import MasterStore
from remi.store.projects import ProjectStore
from remi.store.servers import Server, ServerStore
from remi.sync.metadata import refresh_metadata

log = logging.getLogger(__name__)

CURSOR_OVERLAP = timedelta(minutes=2)
DELETE_CHECK_BATCH = 50


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def format_ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class NewComment:
    issue_id: int
    journal_id: int
    user_name: str
    notes: str
    subject: str
    project_id: int


@dataclass
class SyncResult:
    changed_issue_ids: set[int] = field(default_factory=set)
    new_comments: list[NewComment] = field(default_factory=list)
    unmonitored_issue_ids: set[int] = field(default_factory=set)
    deleted_issue_ids: set[int] = field(default_factory=set)

    def merge(self, other: SyncResult) -> None:
        self.changed_issue_ids |= other.changed_issue_ids
        self.new_comments.extend(other.new_comments)
        self.unmonitored_issue_ids |= other.unmonitored_issue_ids
        self.deleted_issue_ids |= other.deleted_issue_ids

    @property
    def empty(self) -> bool:
        return not (self.changed_issue_ids or self.new_comments or self.unmonitored_issue_ids
                    or self.deleted_issue_ids)


class ServerSync:
    def __init__(self, db: Database, server_id: int, client: RedmineClient,
                 closed_retention_days: int = 7) -> None:
        self.db = db
        self.server_id = server_id
        self.client = client
        self.closed_retention_days = closed_retention_days
        self.servers = ServerStore(db)
        self.issues = IssueStore(db)
        self.projects = ProjectStore(db)
        self.masters = MasterStore(db)
        # journal を取得したときの updated_on。updated_on は秒単位なので、取得と同じ秒に入った更新は
        # updated_on が変わらず見逃してしまう。開いているチケットは、取得後に一度だけ確認し直す。
        self._unconfirmed: dict[int, str | None] = {}

    @property
    def server(self) -> Server:
        s = self.servers.get(self.server_id)
        if s is None:
            raise LookupError(f"server {self.server_id} was deleted")
        return s

    # --- メタデータ -------------------------------------------------------------
    def refresh_metadata(self) -> None:
        refresh_metadata(self.client, self.db, self.server_id)

    def refresh_identity(self) -> None:
        """自分のユーザーIDと所属グループを更新する(グループ経由の担当の判定に使う)。"""
        u = self.client.current_user()
        name = " ".join(p for p in (u.get("lastname"), u.get("firstname")) if p) or u.get("login")
        self.servers.update_fields(self.server_id, my_user_id=int(u["id"]), my_login=u.get("login"),
                                   my_name=name, my_group_ids=[int(g["id"]) for g in u.get("groups", [])])

    # --- 判定 -----------------------------------------------------------------
    def _closed_ids(self) -> set[int]:
        return self.masters.closed_status_ids(self.server_id)

    def _is_monitored(self, data: dict[str, Any], flags: int, closed_ids: set[int],
                      now: datetime | None = None) -> bool:
        if not flags:
            return False
        status = data.get("status") or {}
        closed = bool(status.get("is_closed")) or status.get("id") in closed_ids
        if not closed:
            return True
        closed_on = parse_ts(data.get("closed_on") or data.get("updated_on"))
        if closed_on is None:
            return False
        now = now or datetime.now(UTC)
        return now - closed_on <= timedelta(days=self.closed_retention_days)

    def _fetch_lists(self, extra: dict[str, Any]) -> tuple[dict[int, dict], dict[int, int]]:
        """担当・ウォッチ・(作成)のクエリを別々に呼び、IDで統合する(設計書 §4.2)。"""
        server = self.server
        authored_projects = self.projects.projects_with_setting(self.server_id, "watch_authored")
        queries: list[tuple[int, dict[str, Any]]] = [
            (REL_ASSIGNED, {"assigned_to_id": "me"}),
            (REL_WATCHING, {"watcher_id": "me"}),
        ]
        if authored_projects:
            queries.append((REL_AUTHORED, {"author_id": "me"}))
        issues: dict[int, dict] = {}
        flags: dict[int, int] = {}
        for flag, q in queries:
            for it in self.client.issues(**q, **extra, sort="updated_on"):
                iid = int(it["id"])
                if flag == REL_AUTHORED and int(it["project"]["id"]) not in authored_projects:
                    continue
                issues[iid] = it
                f = flag
                if flag == REL_ASSIGNED:
                    aid = (it.get("assigned_to") or {}).get("id")
                    if aid is not None and aid != server.my_user_id and aid in server.my_group_ids:
                        f |= REL_GROUP
                flags[iid] = flags.get(iid, 0) | f
        return issues, flags

    def _store_issue(self, data: dict, flags: int, closed_ids: set[int], baseline: str | None) -> bool:
        """チケットを保存し、更新があれば True を返す。"""
        iid = int(data["id"])
        before = self.issues.updated_on(self.server_id, iid)
        proj = data.get("project") or {}
        if self.projects.get(self.server_id, int(proj["id"])) is None:
            self.projects.ensure_placeholder(self.server_id, int(proj["id"]), proj.get("name", ""))
        self.issues.upsert_issue(self.server_id, data, flags, self._is_monitored(data, flags, closed_ids))
        self.issues.ensure_read_state(self.server_id, iid, baseline)
        for kind, key in (("user", "assigned_to"), ("user", "author")):
            ref = data.get(key) or {}
            self.masters.remember(self.server_id, kind, ref.get("id"), ref.get("name"))
        return before != data.get("updated_on")

    # --- 差分同期 ---------------------------------------------------------------
    def initial_sync(self) -> SyncResult:
        """初回同期: 未完了のチケットを全件取得し、すべて既読として扱う(設計書 §4.4)。"""
        result = SyncResult()
        issues, flags = self._fetch_lists({"status_id": "open"})
        closed_ids = self._closed_ids()
        cursor = None
        for iid, data in issues.items():
            # 初回は、そのチケットの現在の更新日時以前をすべて既読にする
            self._store_issue(data, flags[iid], closed_ids, baseline=data.get("updated_on"))
            result.changed_issue_ids.add(iid)
            cursor = max(cursor or "", data.get("updated_on") or "")
        self.servers.update_fields(self.server_id, initial_sync_done=True,
                                   last_sync_cursor=cursor or format_ts(datetime.now(UTC)))
        return result

    def sync_changes(self) -> SyncResult:
        """前回以降に更新されたチケットだけを取得する(設計書 §4.1, §4.2)。"""
        server = self.server
        if not server.initial_sync_done:
            return self.initial_sync()
        cursor = parse_ts(server.last_sync_cursor) or datetime.now(UTC)
        since = format_ts(cursor - CURSOR_OVERLAP)
        issues, flags = self._fetch_lists({"status_id": "*", "updated_on": f">={since}"})
        closed_ids = self._closed_ids()
        result = SyncResult()
        new_cursor = server.last_sync_cursor or since
        for iid, data in issues.items():
            # 新しく見つかったチケットは、前回の同期時刻より後の journal だけを未読にする
            if self._store_issue(data, flags[iid], closed_ids, baseline=since):
                result.changed_issue_ids.add(iid)
            new_cursor = max(new_cursor, data.get("updated_on") or "")
        self.servers.update_fields(self.server_id, last_sync_cursor=new_cursor)
        return result

    # --- journal -------------------------------------------------------------
    def fetch_journals(self, issue_id: int, notify: bool = True) -> SyncResult:
        """チケットの履歴と添付を取得する(設計書 §4.3)。"""
        result = SyncResult()
        existing = self.issues.get(self.server_id, issue_id)
        if existing is None:
            return result  # 一覧の同期で保存していないチケット(関係がない・削除された)の履歴は保存しない
        data = self.client.issue(issue_id, include="journals,attachments")
        first_fetch = not existing.journals_synced_on
        self.issues.upsert_issue(self.server_id, data, existing.relation_flags,
                                 self._is_monitored(data, existing.relation_flags, self._closed_ids()))
        new_ids = self.issues.upsert_journals(self.server_id, issue_id, data.get("journals") or [])
        self.issues.upsert_attachments(self.server_id, issue_id, data.get("attachments") or [])
        for j in data.get("journals") or []:
            u = j.get("user") or {}
            self.masters.remember(self.server_id, "user", u.get("id"), u.get("name"))
        for cf in data.get("custom_fields") or []:
            self.masters.remember(self.server_id, "custom_field", cf.get("id"), cf.get("name"))
        if first_fetch:
            self.issues.apply_read_baseline(self.server_id, issue_id)
        self._unconfirmed[issue_id] = data.get("updated_on")
        if new_ids:
            result.changed_issue_ids.add(issue_id)
        if notify and new_ids:
            me = self.server.my_user_id
            last_read = self.issues.last_read(self.server_id, issue_id)
            for j in self.issues.get_journals(self.server_id, new_ids):
                if j.notes and j.user_id != me and j.journal_id > last_read:
                    result.new_comments.append(NewComment(
                        issue_id, j.journal_id, j.user_name or "", j.notes, data.get("subject", ""),
                        int(data["project"]["id"])))
        return result

    def check_issue(self, issue_id: int) -> SyncResult:
        """開いているチケットの軽い確認: updated_on が変わっていたら取得し直す(設計書 §4.5)。"""
        data = self.client.issue(issue_id)
        updated_on = data.get("updated_on")
        unchanged = updated_on == self.issues.updated_on(self.server_id, issue_id) \
            and self.issues.journals_synced(self.server_id, issue_id)
        if unchanged and self._unconfirmed.get(issue_id, "") != updated_on:
            return SyncResult()
        result = self.fetch_journals(issue_id)
        if unchanged:
            self._unconfirmed.pop(issue_id, None)  # 確認できた: 次からは updated_on だけで判定する
        return result

    def pending_journal_issue_ids(self, limit: int = 20) -> list[int]:
        return self.issues.issues_needing_journals(self.server_id, limit)

    def set_watching_flag(self, issue_id: int, watching: bool) -> None:
        """ウォッチの追加・解除をローカルの関係フラグにも反映する。"""
        row = self.issues.get(self.server_id, issue_id)
        if row is None:
            return
        flags = (row.relation_flags | REL_WATCHING) if watching else (row.relation_flags & ~REL_WATCHING)
        self.issues.set_relation_flags(self.server_id, issue_id, flags)
        if not flags:
            self.issues.set_monitored(self.server_id, [issue_id], False)
        elif not row.monitored and watching:
            self.issues.set_monitored(self.server_id, [issue_id], True)

    # --- 関係が外れた・クローズ・削除(設計書 §4.7) ------------------------------
    def reconcile(self) -> SyncResult:
        """未完了のチケットの一覧を取り直し、関係が外れたチケットを監視対象外にする。"""
        result = SyncResult()
        issues, flags = self._fetch_lists({"status_id": "open"})
        closed_ids = self._closed_ids()
        for iid, data in issues.items():
            row = self.issues.get(self.server_id, iid)
            if row is None or row.relation_flags != flags[iid] or not row.monitored:
                self._store_issue(data, flags[iid], closed_ids, baseline=self.server.last_sync_cursor)
                result.changed_issue_ids.add(iid)
        now = datetime.now(UTC)
        drop = []
        for iid in self.issues.monitored_ids(self.server_id) - set(issues):
            row = self.issues.get(self.server_id, iid)
            closed = row.status_id in closed_ids
            closed_on = parse_ts(row.closed_on or row.updated_on)
            if closed and closed_on and now - closed_on <= timedelta(days=self.closed_retention_days):
                continue  # クローズ後の保持期間内
            drop.append(iid)
        if drop:
            self.issues.set_monitored(self.server_id, drop, False)
            result.unmonitored_issue_ids |= set(drop)
        return result

    def detect_deleted(self) -> SyncResult:
        """監視対象のチケットが削除されていないか確認する。見えなくなったものは個別に404を確認する。"""
        result = SyncResult()
        ids = sorted(self.issues.monitored_ids(self.server_id))
        for i in range(0, len(ids), DELETE_CHECK_BATCH):
            batch = ids[i:i + DELETE_CHECK_BATCH]
            found = {int(it["id"]) for it in self.client.issues(
                issue_id=",".join(map(str, batch)), status_id="*")}
            for iid in set(batch) - found:
                try:
                    self.client.issue(iid)
                except RedmineError as e:
                    if e.code == ErrorCode.NOT_FOUND:
                        result.deleted_issue_ids.add(iid)
                    elif e.code == ErrorCode.FORBIDDEN:  # 権限がなくなった: 監視対象外にする
                        result.unmonitored_issue_ids.add(iid)
                    else:
                        raise
        if result.deleted_issue_ids:
            self.issues.delete_issues(self.server_id, sorted(result.deleted_issue_ids))
        if result.unmonitored_issue_ids:
            self.issues.set_monitored(self.server_id, sorted(result.unmonitored_issue_ids), False)
        return result
