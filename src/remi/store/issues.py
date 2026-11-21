"""チケット・journal・添付・既読状態・下書きの読み書き(設計書 §3.4, §4, §7)。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from remi.store.db import Database

# relation_flags のビット
REL_ASSIGNED = 1
REL_WATCHING = 2
REL_AUTHORED = 4
REL_GROUP = 8  # グループ経由の担当

# 表示するチケットの選択(設計書 §5.1)に使う関係の名前。担当は「自分」と「グループ経由」を分けて選べる
RELATIONS = ("assigned", "group", "watching", "authored")
_RELATION_SQL = {
    "assigned": f"(i.relation_flags & {REL_ASSIGNED} AND NOT i.relation_flags & {REL_GROUP})",
    "group": f"(i.relation_flags & {REL_GROUP})",
    "watching": f"(i.relation_flags & {REL_WATCHING})",
    "authored": f"(i.relation_flags & {REL_AUTHORED})",
}


# 関係が外れた未完了のチケットのキャッシュを残す日数(監視対象外になってから)
PURGE_DAYS = 10


# 「表示するチケット」の既定。「自分が作成」は含めない(含めたプロジェクトでは、作成したチケットも取得する。通信が増えるため)
DEFAULT_RELATIONS = ("assigned", "group", "watching")

# 一覧の絞り込みボタン(設定 relation_filter)が選ぶ関係。グループ経由の担当は「担当」に含める
FILTER_RELATIONS: dict[str, tuple[str, ...] | None] = {
    "all": None, "assigned": ("assigned", "group"), "watching": ("watching",), "authored": ("authored",),
}


@dataclass(frozen=True)
class RelationScope:
    """一覧に出すチケット: 「表示するチケット」(プロジェクトの設定 → 親プロジェクト → サーバーの既定 の順に
    解決したもの)を、一覧の絞り込みボタン(relation_filter)でさらに絞ったもの。

    一覧・ツリーの件数と未読数・トレイの未読数・通知で同じものを使い、数を一致させる。
    servers はサーバーの既定の関係(server_id → 関係)、projects はプロジェクトで設定している(継承を含む)関係
    ((server_id, project_id) → 関係)。どちらも空なら何も出さない。どちらにもなければ default。
    in_use は、監視中のチケットがあるプロジェクトで表示している関係の和(画面で出す絞り込みボタンを決める)。
    """

    default: tuple[str, ...] = DEFAULT_RELATIONS
    servers: dict[int, tuple[str, ...]] = field(default_factory=dict)
    projects: dict[tuple[int, int], tuple[str, ...]] = field(default_factory=dict)
    in_use: tuple[str, ...] = RELATIONS

    @classmethod
    def build(cls, server_defaults: dict[int, list[str] | None], visible: dict[int, dict[int, list[str]]],
              in_use: set[str], relation_filter: str | None = None) -> RelationScope:
        """server_defaults: server_id → サーバーの既定(None はすべて)。
        visible: server_id → (project_id → 表示するチケット)。プロジェクトで設定していないものは含めない。

        どのプロジェクトでも表示しない関係で絞り込んでいたら(画面ではボタンが隠れる)「すべて」として扱う。
        """
        used = tuple(r for r in RELATIONS if r in in_use) or RELATIONS
        want = FILTER_RELATIONS.get(relation_filter or "all")
        if want is not None and not set(want) & set(used):
            want = None

        def narrow(rels) -> tuple[str, ...]:
            return tuple(r for r in RELATIONS if r in rels and (want is None or r in want))

        return cls(
            default=narrow(DEFAULT_RELATIONS),
            servers={sid: narrow(rels) for sid, rels in server_defaults.items() if rels is not None},
            projects={(sid, pid): narrow(rels) for sid, m in visible.items() for pid, rels in m.items()},
            in_use=used,
        )

    def relations_for(self, server_id: int, project_id: int) -> tuple[str, ...]:
        return self.projects.get((server_id, project_id), self.servers.get(server_id, self.default))

    def matches(self, server_id: int, project_id: int, flags: int) -> bool:
        return matches_relations(flags, list(self.relations_for(server_id, project_id)))

    def sql(self) -> str:
        """チケット(別名 i)をこの範囲に絞るSQLの条件。値はすべて整数と固定の文字列なので、そのまま埋め込む。"""
        default = relation_condition(self.default)
        if not self.projects and all(v == self.default for v in self.servers.values()):
            return default
        server_ids = sorted({int(sid) for sid in self.servers} | {int(sid) for sid, _ in self.projects})
        parts = []
        for sid in server_ids:
            groups: dict[tuple[str, ...], list[int]] = {}
            for (s_, pid), rels in self.projects.items():
                if s_ == sid:
                    groups.setdefault(rels, []).append(int(pid))
            in_ = lambda pids: f"i.project_id IN ({','.join(map(str, sorted(pids)))})"  # noqa: E731
            sub = [f"({in_(pids)} AND {relation_condition(rels)})" for rels, pids in sorted(groups.items())]
            fallback = relation_condition(self.servers.get(sid, self.default))
            own = [pid for pids in groups.values() for pid in pids]
            sub.append(f"(NOT {in_(own)} AND {fallback})" if own else fallback)
            parts.append(f"(i.server_id = {sid} AND ({' OR '.join(sub)}))")
        parts.append(f"(i.server_id NOT IN ({','.join(map(str, server_ids))}) AND {default})")
        return f"({' OR '.join(parts)})"


def _scope_sql(relations: list[str] | RelationScope | None) -> str:
    return relations.sql() if isinstance(relations, RelationScope) else relation_condition(relations)


def matches_relations(flags: int, relations: list[str] | None) -> bool:
    """relation_flags が、relations のどれかの関係を持つか(relation_condition と同じ判定をPythonで行う)。"""
    if relations is None or set(RELATIONS) <= set(relations):
        return True
    has = {
        "assigned": bool(flags & REL_ASSIGNED) and not flags & REL_GROUP,
        "group": bool(flags & REL_GROUP),
        "watching": bool(flags & REL_WATCHING),
        "authored": bool(flags & REL_AUTHORED),
    }
    return any(has[r] for r in relations if r in has)


def relation_condition(include: list[str] | tuple[str, ...] | set[str] | None) -> str:
    """表示する関係(include)のどれかを持つチケットに絞るSQLの条件。None やすべての関係なら絞らない。"""
    if include is None or set(RELATIONS) <= set(include):
        return "1=1"
    parts = [_RELATION_SQL[r] for r in RELATIONS if r in include]
    return f"({' OR '.join(parts)})" if parts else "0=1"


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class IssueRow:
    server_id: int
    issue_id: int
    project_id: int
    tracker_id: int | None
    subject: str
    description: str | None
    status_id: int | None
    priority_id: int | None
    assigned_to_id: int | None
    assigned_to_name: str | None
    author_id: int | None
    author_name: str | None
    created_on: str | None
    updated_on: str | None
    closed_on: str | None
    relation_flags: int
    monitored: bool
    journals_synced_on: str | None
    unread: int = 0
    last_note_user: str | None = None
    last_note: str | None = None


@dataclass
class JournalRow:
    server_id: int
    journal_id: int
    issue_id: int
    user_id: int | None
    user_name: str | None
    notes: str
    private_notes: bool
    created_on: str | None
    details: list[dict[str, Any]]


@dataclass
class AttachmentRow:
    server_id: int
    attachment_id: int
    issue_id: int
    filename: str
    content_type: str | None
    filesize: int | None
    content_url: str | None
    local_path: str | None


def _issue(r) -> IssueRow:
    keys = r.keys()
    return IssueRow(
        server_id=r["server_id"], issue_id=r["issue_id"], project_id=r["project_id"],
        tracker_id=r["tracker_id"], subject=r["subject"], description=r["description"],
        status_id=r["status_id"], priority_id=r["priority_id"], assigned_to_id=r["assigned_to_id"],
        assigned_to_name=r["assigned_to_name"], author_id=r["author_id"], author_name=r["author_name"],
        created_on=r["created_on"], updated_on=r["updated_on"], closed_on=r["closed_on"],
        relation_flags=r["relation_flags"], monitored=bool(r["monitored"]),
        journals_synced_on=r["journals_synced_on"],
        unread=r["unread"] if "unread" in keys else 0,
        last_note_user=r["last_note_user"] if "last_note_user" in keys else None,
        last_note=r["last_note"] if "last_note" in keys else None,
    )


def _journal(r) -> JournalRow:
    return JournalRow(
        server_id=r["server_id"], journal_id=r["journal_id"], issue_id=r["issue_id"],
        user_id=r["user_id"], user_name=r["user_name"], notes=r["notes"] or "",
        private_notes=bool(r["private_notes"]), created_on=r["created_on"],
        details=json.loads(r["details_json"] or "[]"),
    )


# 未読数: 最終既読より新しく、notes があり、自分以外が書いた journal の数(設計書 §5, §7.2)
_UNREAD_SQL = """
(SELECT COUNT(*) FROM journals j
  WHERE j.server_id = i.server_id AND j.issue_id = i.issue_id
    AND j.journal_id > COALESCE(rs.last_read_journal_id, 0)
    AND j.notes <> '' AND COALESCE(j.user_id, -1) <> COALESCE(s.my_user_id, -2))
"""
_LAST_NOTE_SQL = """
(SELECT j.{col} FROM journals j
  WHERE j.server_id = i.server_id AND j.issue_id = i.issue_id AND j.notes <> ''
  ORDER BY j.journal_id DESC LIMIT 1)
"""


class IssueStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # --- チケット -------------------------------------------------------------
    def upsert_issue(self, server_id: int, data: dict[str, Any], relation_flags: int,
                     monitored: bool = True) -> None:
        """REST APIのチケット(JSON)を保存する。journals_synced_on は変更しない。"""
        assigned = data.get("assigned_to") or {}
        author = data.get("author") or {}
        with self.db.tx() as c:
            c.execute(
                """INSERT INTO issues(server_id, issue_id, project_id, tracker_id, subject, description,
                       status_id, priority_id, assigned_to_id, assigned_to_name, author_id, author_name,
                       created_on, updated_on, closed_on, relation_flags, monitored, unmonitored_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(server_id, issue_id) DO UPDATE SET
                       project_id=excluded.project_id, tracker_id=excluded.tracker_id,
                       subject=excluded.subject, description=excluded.description,
                       status_id=excluded.status_id, priority_id=excluded.priority_id,
                       assigned_to_id=excluded.assigned_to_id, assigned_to_name=excluded.assigned_to_name,
                       author_id=excluded.author_id, author_name=excluded.author_name,
                       created_on=excluded.created_on, updated_on=excluded.updated_on,
                       closed_on=excluded.closed_on, relation_flags=excluded.relation_flags,
                       monitored=excluded.monitored,
                       unmonitored_at=CASE WHEN excluded.monitored = 1 THEN NULL
                                           ELSE COALESCE(issues.unmonitored_at, excluded.unmonitored_at) END""",
                (
                    server_id, int(data["id"]), int(data["project"]["id"]),
                    (data.get("tracker") or {}).get("id"), data.get("subject", ""), data.get("description"),
                    (data.get("status") or {}).get("id"), (data.get("priority") or {}).get("id"),
                    assigned.get("id"), assigned.get("name"), author.get("id"), author.get("name"),
                    data.get("created_on"), data.get("updated_on"), data.get("closed_on"),
                    relation_flags, int(monitored), None if monitored else utcnow(),
                ),
            )

    def upsert_issue_data(self, server_id: int, data: dict[str, Any]) -> None:
        """自分との関係(relation_flags・監視対象か)を変えずに、チケットの内容だけを保存する。
        まだ保存していなければ、関係のないチケット(監視対象外)として保存する(バージョンのチケットに使う)。
        """
        row = self.get(server_id, int(data["id"]))
        if row is None:
            self.upsert_issue(server_id, data, 0, monitored=False)
        else:
            self.upsert_issue(server_id, data, row.relation_flags, row.monitored)

    def get(self, server_id: int, issue_id: int) -> IssueRow | None:
        r = self.db.query_one("SELECT * FROM issues WHERE server_id = ? AND issue_id = ?", (server_id, issue_id))
        return _issue(r) if r else None

    def updated_on(self, server_id: int, issue_id: int) -> str | None:
        r = self.db.query_one("SELECT updated_on FROM issues WHERE server_id = ? AND issue_id = ?",
                              (server_id, issue_id))
        return r[0] if r else None

    def set_monitored(self, server_id: int, issue_ids: list[int], monitored: bool) -> None:
        with self.db.tx() as c:
            # 監視対象外になった時刻を覚える(キャッシュの削除の猶予に使う)。すでに外れているものは、時刻を変えない
            c.executemany("UPDATE issues SET monitored = ?, "
                          "unmonitored_at = CASE WHEN ? THEN NULL ELSE COALESCE(unmonitored_at, ?) END "
                          "WHERE server_id = ? AND issue_id = ?",
                          [(int(monitored), int(monitored), utcnow(), server_id, i) for i in issue_ids])

    def set_relation_flags(self, server_id: int, issue_id: int, flags: int) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE issues SET relation_flags = ? WHERE server_id = ? AND issue_id = ?",
                      (flags, server_id, issue_id))

    def monitored_ids(self, server_id: int) -> set[int]:
        return {r[0] for r in self.db.query(
            "SELECT issue_id FROM issues WHERE server_id = ? AND monitored = 1", (server_id,))}

    def monitored_relation_counts(self, server_id: int) -> tuple[int, int]:
        """監視対象(未完了)のチケットの、(担当の件数, ウォッチの件数)。サーバーの件数との比較に使う。"""
        r = self.db.query_one(
            "SELECT COALESCE(SUM((relation_flags & ?) != 0), 0), COALESCE(SUM((relation_flags & ?) != 0), 0) "
            "FROM issues WHERE server_id = ? AND monitored = 1",
            (REL_ASSIGNED, REL_WATCHING, server_id))
        return int(r[0]), int(r[1])

    def delete_issues(self, server_id: int, issue_ids: list[int]) -> None:
        """チケットのキャッシュを消す。キャッシュした画像のファイルも消す(消さないと、どこからも数えられず残り続ける)。"""
        paths: list[str] = []
        with self.db.tx() as c:
            for iid in issue_ids:
                paths += [r[0] for r in c.execute(
                    "SELECT local_path FROM attachments WHERE server_id = ? AND issue_id = ? "
                    "AND local_path IS NOT NULL", (server_id, iid))]
                for t in ("issues", "journals", "attachments", "read_state", "drafts"):
                    c.execute(f"DELETE FROM {t} WHERE server_id = ? AND issue_id = ?", (server_id, iid))
        for path in paths:
            Path(path).unlink(missing_ok=True)

    def mark_opened(self, server_id: int, issue_id: int) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE issues SET last_opened_at = ? WHERE server_id = ? AND issue_id = ?",
                      (utcnow(), server_id, issue_id))

    def purge_stale(self, days: int = PURGE_DAYS, server_id: int | None = None) -> int:
        """関係が外れた(監視対象外になって days 日経った)未完了のチケットのキャッシュを削除する。

        残すもの: クローズ済み(過去のチケットを見返せるように)、ウォッチしているバージョンに含まれるもの
        (バージョンの一覧に出す)、下書きがあるもの、最近開いたもの。戻ってきたときは、同期が取得し直す。
        """
        rows = self.db.query(
            "SELECT server_id, issue_id FROM issues i WHERE monitored = 0 AND unmonitored_at IS NOT NULL "
            "AND unmonitored_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?) "
            "AND COALESCE(last_opened_at, '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?) "
            "AND (? IS NULL OR server_id = ?) "
            "AND NOT EXISTS (SELECT 1 FROM version_issues v WHERE v.server_id = i.server_id "
            "AND v.issue_id = i.issue_id) "
            "AND NOT EXISTS (SELECT 1 FROM drafts d WHERE d.server_id = i.server_id AND d.issue_id = i.issue_id) "
            "AND NOT EXISTS (SELECT 1 FROM masters m WHERE m.server_id = i.server_id AND m.kind = 'status' "
            "AND m.id = i.status_id AND json_extract(m.extra_json, '$.is_closed'))",
            (f"-{int(days)} days", f"-{int(days)} days", server_id, server_id),
        )
        by_server: dict[int, list[int]] = {}
        for r in rows:
            by_server.setdefault(r[0], []).append(r[1])
        for sid, ids in by_server.items():
            self.delete_issues(sid, ids)
        return len(rows)

    # --- 一覧・未読 -------------------------------------------------------------
    def list_issues(self, server_ids: list[int] | None = None, project_ids: set[int] | None = None,
                    *, include_unmonitored: bool = False, unread_only: bool = False,
                    search: str = "", relations: list[str] | RelationScope | None = None,
                    version_id: int | None = None) -> list[IssueRow]:
        """version_id を渡すと、そのバージョンに含まれるチケット(自分と関係ないものを含む)を返す。"""
        where = [_scope_sql(relations)]
        params: list[Any] = []
        if version_id is not None:
            where.append("EXISTS (SELECT 1 FROM version_issues v WHERE v.server_id = i.server_id "
                         "AND v.issue_id = i.issue_id AND v.version_id = ?)")
            params.append(int(version_id))
        elif not include_unmonitored:
            where.append("i.monitored = 1")
        if server_ids is not None:
            if not server_ids:
                return []
            where.append(f"i.server_id IN ({','.join('?' * len(server_ids))})")
            params.extend(server_ids)
        if project_ids is not None:
            if not project_ids:
                return []
            where.append(f"i.project_id IN ({','.join('?' * len(project_ids))})")
            params.extend(sorted(project_ids))
        if search:
            s = search.strip().lstrip("#")
            if s.isdigit():
                where.append("(i.issue_id = ? OR i.subject LIKE ?)")
                params.extend([int(s), f"%{search.strip()}%"])
            else:
                where.append("i.subject LIKE ?")
                params.append(f"%{search.strip()}%")
        sql = f"""
            SELECT i.*, {_UNREAD_SQL} AS unread,
                   {_LAST_NOTE_SQL.format(col='user_name')} AS last_note_user,
                   {_LAST_NOTE_SQL.format(col='notes')} AS last_note
            FROM issues i
            JOIN servers s ON s.server_id = i.server_id
            LEFT JOIN read_state rs ON rs.server_id = i.server_id AND rs.issue_id = i.issue_id
            WHERE {' AND '.join(where)}
            ORDER BY i.updated_on DESC
        """
        rows = [_issue(r) for r in self.db.query(sql, params)]
        if unread_only:
            rows = [r for r in rows if r.unread > 0]
        return rows

    def unread_by_project(self, server_id: int, relations: list[str] | RelationScope | None = None) -> dict[int, int]:
        """プロジェクトごとの未読数。relations(関係または RelationScope)を渡すと、そのチケットだけを数える。"""
        sql = f"""
            SELECT i.project_id, SUM({_UNREAD_SQL}) FROM issues i
            JOIN servers s ON s.server_id = i.server_id
            LEFT JOIN read_state rs ON rs.server_id = i.server_id AND rs.issue_id = i.issue_id
            WHERE i.server_id = ? AND i.monitored = 1 AND {_scope_sql(relations)}
            GROUP BY i.project_id
        """
        return {r[0]: int(r[1] or 0) for r in self.db.query(sql, (server_id,))}

    def count_by_project(self, server_id: int, relations: list[str] | RelationScope | None = None) -> dict[int, int]:
        """プロジェクトごとの件数。relations(関係または RelationScope)を渡すと、そのチケットだけを数える。"""
        return {r[0]: r[1] for r in self.db.query(
            f"SELECT i.project_id, COUNT(*) FROM issues i WHERE i.server_id = ? AND i.monitored = 1 "
            f"AND {_scope_sql(relations)} GROUP BY i.project_id",
            (server_id,))}

    def unread_count(self, server_id: int, issue_id: int) -> int:
        r = self.db.query_one(
            f"""SELECT {_UNREAD_SQL} FROM issues i JOIN servers s ON s.server_id = i.server_id
                LEFT JOIN read_state rs ON rs.server_id = i.server_id AND rs.issue_id = i.issue_id
                WHERE i.server_id = ? AND i.issue_id = ?""",
            (server_id, issue_id),
        )
        return int(r[0]) if r else 0

    # --- journal --------------------------------------------------------------
    def upsert_journals(self, server_id: int, issue_id: int, journals: list[dict[str, Any]]) -> list[int]:
        """チケットの journal をすべて受け取って保存し、新しく追加された journal_id を返す。
        notes・プライベートが変わったものは更新し、Redmineで削除された(届かなかった)ものは消す。
        """
        existing = {r[0]: (r[1], bool(r[2])) for r in self.db.query(
            "SELECT journal_id, notes, private_notes FROM journals WHERE server_id = ? AND issue_id = ?",
            (server_id, issue_id))}
        new_ids: list[int] = []
        with self.db.tx() as c:
            for jid in existing.keys() - {int(j["id"]) for j in journals}:
                c.execute("DELETE FROM journals WHERE server_id = ? AND journal_id = ?", (server_id, jid))
            for j in journals:
                jid = int(j["id"])
                user = j.get("user") or {}
                notes = j.get("notes") or ""
                private = bool(j.get("private_notes"))
                if jid in existing:
                    if existing[jid] != (notes, private):
                        c.execute("UPDATE journals SET notes = ?, private_notes = ?, updated_on = ? "
                                  "WHERE server_id = ? AND journal_id = ?",
                                  (notes, int(private), j.get("updated_on"), server_id, jid))
                    continue
                c.execute(
                    """INSERT INTO journals(server_id, journal_id, issue_id, user_id, user_name, notes,
                           private_notes, created_on, updated_on, details_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (server_id, jid, issue_id, user.get("id"), user.get("name"), notes,
                     int(bool(j.get("private_notes"))), j.get("created_on"), j.get("updated_on"),
                     json.dumps(j.get("details") or [], ensure_ascii=False)),
                )
                new_ids.append(jid)
            c.execute("UPDATE issues SET journals_synced_on = updated_on WHERE server_id = ? AND issue_id = ?",
                      (server_id, issue_id))
        return new_ids

    def journals(self, server_id: int, issue_id: int, limit: int | None = None) -> list[JournalRow]:
        if limit is None:
            rows = self.db.query("SELECT * FROM journals WHERE server_id = ? AND issue_id = ? ORDER BY journal_id",
                                 (server_id, issue_id))
        else:
            rows = self.db.query(
                "SELECT * FROM (SELECT * FROM journals WHERE server_id = ? AND issue_id = ? "
                "ORDER BY journal_id DESC LIMIT ?) ORDER BY journal_id", (server_id, issue_id, limit))
        return [_journal(r) for r in rows]

    def journal_count(self, server_id: int, issue_id: int) -> int:
        return self.db.query_one("SELECT COUNT(*) FROM journals WHERE server_id = ? AND issue_id = ?",
                                 (server_id, issue_id))[0]

    def get_journals(self, server_id: int, journal_ids: list[int]) -> list[JournalRow]:
        if not journal_ids:
            return []
        marks = ",".join("?" * len(journal_ids))
        return [_journal(r) for r in self.db.query(
            f"SELECT * FROM journals WHERE server_id = ? AND journal_id IN ({marks}) ORDER BY journal_id",
            (server_id, *journal_ids))]

    def latest_journal_id(self, server_id: int, issue_id: int) -> int:
        r = self.db.query_one("SELECT MAX(journal_id) FROM journals WHERE server_id = ? AND issue_id = ?",
                              (server_id, issue_id))
        return int(r[0] or 0)

    def issues_needing_journals(self, server_id: int, limit: int = 20) -> list[int]:
        """journal を未取得、または更新後に未取得のチケット(遅延取得キュー用)。"""
        return [r[0] for r in self.db.query(
            "SELECT issue_id FROM issues WHERE server_id = ? AND monitored = 1 AND "
            "(journals_synced_on IS NULL OR journals_synced_on <> updated_on) "
            "ORDER BY updated_on DESC LIMIT ?", (server_id, limit))]

    # --- 添付 -----------------------------------------------------------------
    def upsert_attachments(self, server_id: int, issue_id: int, attachments: list[dict[str, Any]]) -> list[str]:
        """チケットの添付をすべて受け取って保存する。Redmineで削除されたものは消し、
        その画像キャッシュのファイルのパスを返す(呼び出し側で消す)。
        """
        keep = {int(a["id"]) for a in attachments}
        removed = [(r[0], r[1]) for r in self.db.query(
            "SELECT attachment_id, local_path FROM attachments WHERE server_id = ? AND issue_id = ?",
            (server_id, issue_id)) if r[0] not in keep]
        with self.db.tx() as c:
            for aid, _ in removed:
                c.execute("DELETE FROM attachments WHERE server_id = ? AND attachment_id = ?", (server_id, aid))
            for a in attachments:
                c.execute(
                    """INSERT INTO attachments(server_id, attachment_id, issue_id, filename, content_type,
                           filesize, content_url, description, author_name, created_on)
                       VALUES(?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(server_id, attachment_id) DO UPDATE SET
                           filename=excluded.filename, content_type=excluded.content_type,
                           filesize=excluded.filesize, content_url=excluded.content_url,
                           description=excluded.description""",
                    (server_id, int(a["id"]), issue_id, a.get("filename", ""), a.get("content_type"),
                     a.get("filesize"), a.get("content_url"), a.get("description"),
                     (a.get("author") or {}).get("name"), a.get("created_on")),
                )
        return [path for _, path in removed if path]

    def attachments(self, server_id: int, issue_id: int) -> list[AttachmentRow]:
        return [AttachmentRow(r["server_id"], r["attachment_id"], r["issue_id"], r["filename"], r["content_type"],
                              r["filesize"], r["content_url"], r["local_path"])
                for r in self.db.query("SELECT * FROM attachments WHERE server_id = ? AND issue_id = ? "
                                       "ORDER BY attachment_id", (server_id, issue_id))]

    def get_attachment(self, server_id: int, attachment_id: int) -> AttachmentRow | None:
        r = self.db.query_one("SELECT * FROM attachments WHERE server_id = ? AND attachment_id = ?",
                              (server_id, attachment_id))
        if not r:
            return None
        return AttachmentRow(r["server_id"], r["attachment_id"], r["issue_id"], r["filename"], r["content_type"],
                             r["filesize"], r["content_url"], r["local_path"])

    def set_attachment_cached(self, server_id: int, attachment_id: int, local_path: str | None) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE attachments SET local_path = ?, cached_at = ? WHERE server_id = ? AND attachment_id = ?",
                      (local_path, utcnow() if local_path else None, server_id, attachment_id))

    def cached_attachments_oldest_first(self) -> list[tuple[int, int, str]]:
        return [(r[0], r[1], r[2]) for r in self.db.query(
            "SELECT server_id, attachment_id, local_path FROM attachments WHERE local_path IS NOT NULL "
            "ORDER BY cached_at")]

    # --- 既読 -----------------------------------------------------------------
    def last_notified(self, server_id: int, issue_id: int) -> int:
        """ここまでの journal は通知した(同じコメントを2回通知しない)。"""
        r = self.db.query_one("SELECT journal_id FROM notified WHERE server_id = ? AND issue_id = ?",
                              (server_id, issue_id))
        return int(r[0]) if r else 0

    def set_notified(self, server_id: int, issue_id: int, journal_id: int) -> None:
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO notified(server_id, issue_id, journal_id) VALUES(?, ?, ?) "
                "ON CONFLICT(server_id, issue_id) DO UPDATE SET journal_id = MAX(journal_id, excluded.journal_id)",
                (server_id, issue_id, int(journal_id)))

    def last_read(self, server_id: int, issue_id: int) -> int:
        r = self.db.query_one("SELECT last_read_journal_id FROM read_state WHERE server_id = ? AND issue_id = ?",
                              (server_id, issue_id))
        return int(r[0]) if r else 0

    def mark_read(self, server_id: int, issue_id: int, journal_id: int | None = None) -> None:
        """journal_id まで既読にする(省略時は保存済みの最新まで)。既読位置は戻さない。"""
        if journal_id is None:
            journal_id = self.latest_journal_id(server_id, issue_id)
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO read_state(server_id, issue_id, last_read_journal_id, read_at) VALUES(?,?,?,?) "
                "ON CONFLICT(server_id, issue_id) DO UPDATE SET "
                "last_read_journal_id = MAX(last_read_journal_id, excluded.last_read_journal_id), "
                "read_at = excluded.read_at",
                (server_id, issue_id, journal_id, utcnow()),
            )

    def ensure_read_state(self, server_id: int, issue_id: int, baseline: str | None) -> None:
        """初めて見つけたチケットの既読状態を作る。baseline 以前の journal は後で既読にする。"""
        with self.db.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO read_state(server_id, issue_id, last_read_journal_id, read_baseline) "
                "VALUES(?, ?, 0, ?)", (server_id, issue_id, baseline))

    def apply_read_baseline(self, server_id: int, issue_id: int) -> None:
        """基準時刻以前に作られた journal を既読にし、基準時刻を消す(初回の journal 取得後に呼ぶ)。"""
        r = self.db.query_one("SELECT read_baseline FROM read_state WHERE server_id = ? AND issue_id = ?",
                              (server_id, issue_id))
        if not r or r[0] is None:
            return
        m = self.db.query_one(
            "SELECT MAX(journal_id) FROM journals WHERE server_id = ? AND issue_id = ? AND created_on <= ?",
            (server_id, issue_id, r[0]))
        last = int(m[0] or 0)
        with self.db.tx() as c:
            c.execute("UPDATE read_state SET last_read_journal_id = MAX(last_read_journal_id, ?), "
                      "read_baseline = NULL WHERE server_id = ? AND issue_id = ?", (last, server_id, issue_id))

    def journals_synced(self, server_id: int, issue_id: int) -> bool:
        r = self.db.query_one("SELECT journals_synced_on FROM issues WHERE server_id = ? AND issue_id = ?",
                              (server_id, issue_id))
        return bool(r and r[0])

    def mark_unread(self, server_id: int, issue_id: int) -> None:
        """最後のコメントの直前まで既読を戻す。"""
        r = self.db.query_one(
            "SELECT journal_id FROM journals WHERE server_id = ? AND issue_id = ? AND notes <> '' "
            "ORDER BY journal_id DESC LIMIT 1", (server_id, issue_id))
        target = (int(r[0]) - 1) if r else 0
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO read_state(server_id, issue_id, last_read_journal_id, read_at) VALUES(?,?,?,?) "
                "ON CONFLICT(server_id, issue_id) DO UPDATE SET last_read_journal_id = excluded.last_read_journal_id",
                (server_id, issue_id, target, utcnow()),
            )

    def mark_all_read(self, server_id: int, project_ids: set[int] | None = None) -> None:
        sql = "SELECT issue_id FROM issues WHERE server_id = ? AND monitored = 1"
        params: list[Any] = [server_id]
        if project_ids is not None:
            if not project_ids:
                return
            sql += f" AND project_id IN ({','.join('?' * len(project_ids))})"
            params.extend(sorted(project_ids))
        for r in self.db.query(sql, params):
            self.mark_read(server_id, r[0])

    # --- 下書き ---------------------------------------------------------------
    def get_draft(self, server_id: int, issue_id: int) -> tuple[str, bool, str | None]:
        r = self.db.query_one("SELECT body, private_notes, failed_at FROM drafts WHERE server_id = ? AND issue_id = ?",
                              (server_id, issue_id))
        if not r:
            return "", False, None
        return r[0], bool(r[1]), r[2]

    def save_draft(self, server_id: int, issue_id: int, body: str, private: bool,
                   failed: bool = False, error: str | None = None) -> None:
        with self.db.tx() as c:
            if not body and not failed:
                c.execute("DELETE FROM drafts WHERE server_id = ? AND issue_id = ?", (server_id, issue_id))
                return
            c.execute(
                "INSERT INTO drafts(server_id, issue_id, body, private_notes, failed_at, error, updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(server_id, issue_id) DO UPDATE SET body=excluded.body, "
                "private_notes=excluded.private_notes, failed_at=excluded.failed_at, error=excluded.error, "
                "updated_at=excluded.updated_at",
                (server_id, issue_id, body, int(private), utcnow() if failed else None, error, utcnow()),
            )

    def clear_draft(self, server_id: int, issue_id: int) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM drafts WHERE server_id = ? AND issue_id = ?", (server_id, issue_id))
