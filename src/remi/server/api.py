"""画面(JavaScript)から呼べるAPI(設計書 §2.6)。

画面から呼べるのは @rpc を付けたメソッドだけ。引数と戻り値はJSONで表せる値にする。
エラーは RpcError(コード) で返し、文言は画面側で翻訳する。
主な戻り値の形は src/remi/web/js/types.js に書いてある。項目を変えたら types.js も直すこと
(tests/test_types_doc.py で一致を確認する)。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from typing import Any, Protocol

from remi import __version__
from remi.api.client import RedmineClient, normalize_url
from remi.api.errors import ErrorCode, RedmineError
from remi.api.probe import probe_server
from remi.context import AppContext
from remi.i18n import SUPPORTED_LANGUAGES, catalog, resolve_language
from remi.paths import attachment_cache_dir
from remi.render.details import DetailNames, describe_detail
from remi.render.markup import TEXT_FORMATS, RenderContext, render
from remi.render.schemes import link_schemes, scheme_of
from remi.server.tree import build_tree
from remi.store.issues import REL_ASSIGNED, REL_AUTHORED, REL_GROUP, REL_WATCHING, RELATIONS
from remi.store.projects import PROJECT_STATUS_ACTIVE, SETTING_KEYS
from remi.store.servers import POLL_INTERVALS, Server

log = logging.getLogger(__name__)

PAGE_SIZE = 50
# 画面から変更できるグローバル設定と、その値の検証
SETTABLE: dict[str, Callable[[Any], bool]] = {
    "theme": lambda v: v in ("system", "light", "dark"),
    "language": lambda v: v == "system" or v in SUPPORTED_LANGUAGES,
    "notify_enabled": lambda v: isinstance(v, bool),
    "autostart": lambda v: isinstance(v, bool),
    "show_closed": lambda v: isinstance(v, bool),
    "unread_first": lambda v: isinstance(v, bool),
    "closed_retention_days": lambda v: isinstance(v, int) and 0 <= v <= 365,
    "allow_external_images": lambda v: isinstance(v, bool),
    "mine_side": lambda v: v in ("right", "left"),
    "pane_sizes": lambda v: isinstance(v, list) and len(v) <= 3 and all(isinstance(x, int | float) for x in v),
    "tree_collapsed": lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v),
    "redmine_open_in": lambda v: v in ("app", "browser"),
    "relation_filter": lambda v: v in RELATION_FILTERS,
    "issue_details_open": lambda v: isinstance(v, bool),
}
# 一覧の絞り込み(設計書 §5.1)
RELATION_FILTERS = {"all": 0, "assigned": REL_ASSIGNED, "watching": REL_WATCHING, "authored": REL_AUTHORED}
# 画面から変更できる項目(設計書 §6.2)。ヘッダの3項目と、「詳細」で出す項目。カスタムフィールドは "cf:<ID>"
EDITABLE = ("status_id", "assigned_to_id", "priority_id", "tracker_id", "category_id", "fixed_version_id",
            "parent_issue_id", "start_date", "due_date", "done_ratio", "estimated_hours")
_ID_FIELDS = ("status_id", "assigned_to_id", "priority_id", "tracker_id", "category_id", "fixed_version_id",
              "parent_issue_id")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# カスタムフィールドの定義を覚えておく時間
CUSTOM_FIELDS_TTL = 600
# 「詳細」で編集できるカスタムフィールドの形式。ほか(長文・リンク・添付など)は表示だけにする
CF_EDITABLE_FORMATS = ("string", "int", "float", "date", "bool", "list", "enumeration", "user", "version")
MEMBERS_TTL = 600
# 自分のロールと、ロールの権限を覚えておく時間(書き込みできるかの判定に使う)
PERMISSIONS_TTL = 600
# サーバーから取得したデータ(認証ユーザーの権限で見えるもの)と、その利用者の状態を持つテーブル。
# 接続先のURLや認証ユーザーが変わったら捨てる。project_settings は利用者の好みなので残す。
SERVER_CACHE_TABLES = ("issues", "journals", "attachments", "read_state", "drafts", "projects", "masters",
                       "versions", "version_issues", "version_events")
# 初期化の前に同期スレッドを止めるときの待ち時間。取得の通信は止めればすぐ終わる。
# 変更を送っている途中なら、その応答(読み取りのタイムアウトは30秒)を待つ
SYNC_STOP_TIMEOUT = 40.0


def open_with_os(url: str) -> None:
    """URIを、OSでそのスキームに関連付けられたアプリで開く(Windows: ShellExecute)。"""
    if sys.platform == "win32":
        os.startfile(url)  # noqa: S606 - 開けるのは指定したスキームだけ(openUri で確かめる)
    else:
        webbrowser.open(url)


def _valid_change(key: str, v: Any) -> bool:
    """送信前の変更の値を確かめる(Redmineでも検証されるが、明らかにおかしいものはここで断る)。"""
    if key.startswith("cf:"):
        return key[3:].isdigit() and (v is None or isinstance(v, str)
                                      or (isinstance(v, list) and all(isinstance(x, str) for x in v)))
    if key not in EDITABLE:
        return False
    if key in _ID_FIELDS:
        return v is None or (isinstance(v, int) and not isinstance(v, bool) and v > 0)
    if key in ("start_date", "due_date"):
        return v is None or (isinstance(v, str) and bool(_DATE.match(v)))
    if key == "done_ratio":
        return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100
    if key == "estimated_hours":
        return v is None or (isinstance(v, int | float) and not isinstance(v, bool) and 0 <= v < 100000)
    return False


def _valid_relations(v: Any) -> bool:
    """表示するチケット(プロジェクトごと)の値: 関係の重複のないリスト(1つ以上)。"""
    return isinstance(v, list) and len(v) > 0 and len(set(v)) == len(v) and all(x in RELATIONS for x in v)


class RpcError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class WindowController(Protocol):
    def show(self) -> None: ...
    def hide(self) -> None: ...
    def open_redmine(self, url: str) -> bool: ...
    def quit(self) -> None: ...
    def apply_theme(self, theme: str) -> None: ...


class NullWindow:
    """ウィンドウがない場合(ブラウザでの開発モード、テスト)。"""

    def open_redmine(self, url: str) -> bool:
        return False

    def show(self) -> None:
        pass

    def hide(self) -> None:
        pass

    def quit(self) -> None:
        pass

    def apply_theme(self, theme: str) -> None:
        pass


def rpc(fn: Callable) -> Callable:
    fn._rpc = True  # type: ignore[attr-defined]
    return fn


def _flags(flags: int) -> list[str]:
    out = []
    if flags & REL_ASSIGNED:
        out.append("group" if flags & REL_GROUP else "assigned")
    if flags & REL_WATCHING:
        out.append("watching")
    if flags & REL_AUTHORED:
        out.append("authored")
    return out


class RemiApi:
    def __init__(self, ctx: AppContext, sync=None, window: WindowController | None = None,
                 autostart: Callable[[bool], None] | None = None) -> None:
        self.ctx = ctx
        self.sync = sync
        self.window: WindowController = window or NullWindow()
        self.autostart = autostart
        # 通知の判定に使う: 画面で開いているチケットと、ウィンドウがアクティブか
        self.open_issue: tuple[int, int] | None = None
        self.active = True
        self._members: dict[tuple[int, int], tuple[float, list[dict[str, Any]]]] = {}
        # server_id → (取得した時刻, 管理者か, project_id → ロールID)
        self._my_roles: dict[int, tuple[float, bool, dict[int, set[int]]]] = {}
        # (server_id, role_id) → (取得した時刻, 権限)
        self._role_perms: dict[tuple[int, int], tuple[float, set[str]]] = {}
        self._members_lock = threading.Lock()
        # server_id → (取得した時刻, カスタムフィールドの定義。管理者でなく取得できなければ None)
        self._custom_fields: dict[int, tuple[float, list[dict[str, Any]] | None]] = {}

    def methods(self) -> dict[str, Callable]:
        return {name: getattr(self, name) for name in dir(self)
                if not name.startswith("_") and getattr(getattr(self, name), "_rpc", False)}

    @property
    def lang(self) -> str:
        return resolve_language(self.ctx.settings.get("language"))

    # --- 起動 -------------------------------------------------------------------
    @rpc
    def bootstrap(self) -> dict[str, Any]:
        lang = self.lang
        return {
            "version": __version__,
            "lang": lang,
            "languages": SUPPORTED_LANGUAGES,
            "catalog": catalog(lang),
            "fallbackCatalog": catalog("en") if lang != "en" else {},
            "settings": self.getSettings(),
            "hasServers": bool(self.ctx.servers.list()),
            # 本文で自動リンクにするスキーム(src/remi/link_schemes.json)。画面は開く前の確認に使う
            "linkSchemes": [{"scheme": x.scheme, "confirm": x.confirm} for x in link_schemes()],
        }

    # --- 設定 -------------------------------------------------------------------
    @rpc
    def getSettings(self) -> dict[str, Any]:  # noqa: N802
        return {k: self.ctx.settings.get(k) for k in SETTABLE}

    @rpc
    def setSetting(self, key: str, value: Any) -> dict[str, Any]:  # noqa: N802
        check = SETTABLE.get(key)
        if check is None or not check(value):
            raise RpcError("invalid_setting", key)
        self.ctx.settings.set(key, value)
        if key == "theme":
            self.window.apply_theme(value)
        elif key == "autostart" and self.autostart is not None:
            self.autostart(value)
        self.ctx.bus.publish("settingsChanged", key=key)
        return self.getSettings()

    # --- サーバー -----------------------------------------------------------------
    @staticmethod
    def _server_dict(s: Server) -> dict[str, Any]:
        return {
            "serverId": s.server_id, "name": s.name, "url": s.url, "enabled": s.enabled,
            "pollInterval": s.poll_interval, "notifyEnabled": s.notify_enabled, "textFormat": s.text_format,
            "proxyMode": s.proxy_mode, "proxyUrl": s.proxy_url, "myUserId": s.my_user_id, "myLogin": s.my_login,
            "myName": s.my_name, "lastError": s.last_error, "lastSyncedAt": s.last_synced_at,
        }

    @rpc
    def listServers(self) -> list[dict[str, Any]]:  # noqa: N802
        return [self._server_dict(s) for s in self.ctx.servers.list()]

    @rpc
    def testConnection(self, url: str, apiKey: str = "", proxyMode: str = "system",  # noqa: N802,N803
                       proxyUrl: str | None = None, serverId: int | None = None) -> dict[str, Any]:  # noqa: N803
        """接続テスト。編集時にキーを入力し直さなかった場合は、保存済みのキーを使う。"""
        key = apiKey or (self.ctx.servers.api_key(serverId) if serverId else None) or ""
        if not key:
            raise RpcError("unauthorized")
        try:
            client = RedmineClient(url, key, proxy_mode=proxyMode, proxy_url=proxyUrl or None, min_interval=0.2)
        except RedmineError as e:
            raise RpcError(e.code.value, e.detail) from e
        try:
            r = probe_server(client)
        except RedmineError as e:
            raise RpcError(e.code.value, e.detail) from e
        finally:
            client.close()
        g = r.format_guess
        return {
            "url": client.base_url, "userId": r.user_id, "login": r.login, "name": r.name,
            "groupIds": r.group_ids, "assignedCount": r.assigned_count, "watchedCount": r.watched_count,
            "formatGuess": {"textFormat": g.text_format, "samples": g.samples} if g else None,
        }

    @rpc
    def saveServer(self, data: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
        sid = data.get("serverId")
        existing = self.ctx.servers.get(sid) if sid else None
        if sid and existing is None:
            raise RpcError("not_found")
        name = str(data.get("name", "")).strip()
        try:
            url = normalize_url(str(data.get("url", "")))
        except RedmineError as e:
            raise RpcError(e.code.value) from e
        fmt = data.get("textFormat", "common_mark")
        poll = int(data.get("pollInterval", 180))
        proxy_mode = data.get("proxyMode", "system")
        if not name or fmt not in TEXT_FORMATS or poll not in POLL_INTERVALS or proxy_mode not in ("system", "manual"):
            raise RpcError("invalid_input")
        api_key = str(data.get("apiKey") or "").strip() or None
        if existing is None and not api_key:
            raise RpcError("unauthorized")
        if data.get("userId") is None and (existing is None or api_key):
            # 新しいキーは、接続テストで誰のキーかを確かめてから保存する(ユーザーの変更を見逃さないため)
            raise RpcError("not_tested")

        s = existing or Server(name=name, url=url, sort_order=len(self.ctx.servers.list()))
        user_id = int(data["userId"]) if data.get("userId") is not None else None
        url_changed = existing is not None and existing.url != url
        # 同じURLでも、別のユーザーのAPIキーに替えたら前のユーザーのデータ(プライベートノートなど)を見せない。
        # 同じユーザーのキーを替えただけなら残す。ユーザーIDは接続テストに成功したときだけ届く
        user_changed = existing is not None and user_id is not None and existing.my_user_id != user_id
        reset = url_changed or user_changed
        if reset and self.sync is not None:
            # 初期化の後に、動いている同期スレッドが前のデータを書き戻さないよう、先に止める。
            # 止まりきらなければ(書き込みの途中で時間がかかっている)、キャッシュを消さずに切り替えをやめる
            if not self.sync.stop_all(SYNC_STOP_TIMEOUT):
                self._servers_changed()
                raise RpcError("sync_busy")
        s.name, s.url, s.text_format, s.poll_interval = name, url, fmt, poll
        s.notify_enabled = bool(data.get("notifyEnabled", True))
        s.enabled = bool(data.get("enabled", True))
        s.proxy_mode, s.proxy_url = proxy_mode, (data.get("proxyUrl") or None)
        if user_id is not None:
            s.my_user_id = user_id
            s.my_login = data.get("login")
            s.my_name = data.get("userName")
            s.my_group_ids = [int(x) for x in data.get("groupIds", [])]
        if reset:
            # 別のサーバー・別のユーザーになった: キャッシュを捨てて初回同期からやり直す
            s.initial_sync_done, s.last_sync_cursor, s.last_error, s.last_synced_at = False, None, None, None
        try:
            self.ctx.servers.save(s, api_key=api_key)
            if reset:
                self._clear_server_cache(s.server_id)
        finally:
            self._servers_changed()  # 止めた同期スレッドは、失敗しても必ず動かし直す
        return self._server_dict(s)

    def _clear_server_cache(self, server_id: int) -> None:
        """サーバーから取得したデータを、データベース・画像キャッシュ・メモリから消す。"""
        with self.ctx.db.tx() as c:
            for table in SERVER_CACHE_TABLES:
                c.execute(f"DELETE FROM {table} WHERE server_id = ?", (server_id,))
        shutil.rmtree(attachment_cache_dir(server_id), ignore_errors=True)
        with self._members_lock:
            for key in [k for k in self._members if k[0] == server_id]:
                del self._members[key]
            for key in [k for k in self._role_perms if k[0] == server_id]:
                del self._role_perms[key]
            self._my_roles.pop(server_id, None)
            self._custom_fields.pop(server_id, None)  # 前のユーザー(管理者など)の権限で取得した定義
        if self.open_issue is not None and self.open_issue[0] == server_id:
            # 同期を動かし直したときに、前のユーザーで開いていたチケットを取りに行かない
            self.setOpenIssue(None, None)
        # 送信前の添付(LocalServer)と、開いている会話(画面)にも捨てさせる
        self.ctx.bus.publish("serverCacheCleared", serverId=server_id)

    @rpc
    def deleteServer(self, serverId: int) -> None:  # noqa: N802,N803
        if self.ctx.servers.get(serverId) is None:
            raise RpcError("not_found")
        self.ctx.servers.delete(serverId)
        self._servers_changed()

    def _servers_changed(self) -> None:
        if self.sync is not None:
            self.sync.restart()
        self.ctx.bus.publish("serversChanged")

    # --- ツリー・プロジェクト設定 -----------------------------------------------------
    @rpc
    def getTree(self) -> dict[str, Any]:  # noqa: N802
        return build_tree(self.ctx)

    @rpc
    def getProjectSettings(self, serverId: int, projectId: int) -> dict[str, Any]:  # noqa: N802,N803
        out = {}
        server = self.ctx.servers.get(serverId)
        for key in SETTING_KEYS:
            eff = self.ctx.projects.effective_setting(serverId, projectId, key)
            if eff is None and key == "notify":
                eff = bool(server and server.notify_enabled)
            out[key] = {"value": self.ctx.projects.get_setting(serverId, projectId, key), "effective": bool(eff)}
        own = self.ctx.projects.get_visible_relations(serverId, projectId)
        eff_rel = self.ctx.projects.visible_relations_map(serverId).get(
            projectId, (server.visible_relations if server else None) or list(RELATIONS))
        out["visible_relations"] = {"value": own, "effective": [r for r in RELATIONS if r in eff_rel]}
        p = self.ctx.projects.get(serverId, projectId)
        out["url"] = f"{server.url}/projects/{p.identifier or p.project_id}" if server and p else None
        return out

    @rpc
    def setProjectSetting(self, serverId: int, projectId: int, key: str, value: Any) -> dict[str, Any]:  # noqa: N802,N803
        """プロジェクト単位の設定。None は親から継承する。visible_relations は関係のリスト、ほかは真偽値。"""
        if key == "visible_relations":
            if value is not None and not _valid_relations(value):
                raise RpcError("invalid_setting", key)
            self.ctx.projects.set_visible_relations(
                serverId, projectId, None if value is None else [r for r in RELATIONS if r in value])
            self.ctx.bus.publish("projectSettingsChanged", serverId=serverId)
            return self.getProjectSettings(serverId, projectId)
        if key not in SETTING_KEYS or value not in (None, True, False):
            raise RpcError("invalid_setting", key)
        self.ctx.projects.set_setting(serverId, projectId, key, value)
        if key == "watch_versions" and self.sync is not None:
            self.sync.sync_now()  # バージョンをすぐ取得する(やめたときは、ツリーから消す)
        if key == "watch_authored" and self.sync is not None:
            # 作成したチケットを取り直すため、初回同期からやり直す
            self.ctx.servers.update_fields(serverId, initial_sync_done=False)
            self.sync.restart()
        self.ctx.bus.publish("projectSettingsChanged", serverId=serverId)
        return self.getProjectSettings(serverId, projectId)

    @rpc
    def setServerSetting(self, serverId: int, key: str, value: Any) -> None:  # noqa: N802,N803
        """サーバーごとの表示の設定。show_all_projects: チケットのないプロジェクトもツリーに表示するか。
        visible_relations: 表示するチケットの既定(None はすべて。プロジェクトで設定していなければこれを使う)。
        """
        if self.ctx.servers.get(serverId) is None:
            raise RpcError("not_found")
        if key == "show_all_projects" and isinstance(value, bool):
            self.ctx.servers.update_fields(serverId, show_all_projects=value)
        elif key == "visible_relations" and (value is None or _valid_relations(value)):
            self.ctx.servers.update_fields(
                serverId, visible_relations=None if value is None else [r for r in RELATIONS if r in value])
        else:
            raise RpcError("invalid_setting", key)
        self.ctx.bus.publish("projectSettingsChanged", serverId=serverId)

    # --- 一覧 ---------------------------------------------------------------------
    @rpc
    def listIssues(self, serverId: int | None = None, projectId: int | None = None,  # noqa: N802,N803
                   unreadOnly: bool = False, search: str = "", relation: str = "all",  # noqa: N803
                   versionId: int | None = None) -> dict[str, Any]:  # noqa: N803
        """一覧。versionId を渡すと、そのバージョンのチケットをすべて出す(表示するチケット・絞り込みは使わない)。"""
        if versionId is not None:
            return self._list_version_issues(serverId, versionId, search)
        server_ids = [serverId] if serverId else [s.server_id for s in self.ctx.servers.list(enabled_only=True)]
        project_ids = None
        if serverId and projectId:
            project_ids = {projectId} | self.ctx.projects.descendants(serverId, projectId)
        show_closed = bool(self.ctx.settings.get("show_closed"))
        if relation not in RELATION_FILTERS:
            raise RpcError("invalid_input", "relation")
        # プロジェクトごとの表示するチケットと、絞り込みボタンの選択。ツリーの件数・未読数とトレイの未読数も
        # 同じもので数える
        scope = self.ctx.relation_scope(relation)
        rows = self.ctx.issues.list_issues(server_ids, project_ids, include_unmonitored=show_closed,
                                           unread_only=unreadOnly, search=search, relations=scope)
        return self._issue_summaries(rows)

    def _list_version_issues(self, server_id: int | None, version_id: int, search: str) -> dict[str, Any]:
        if server_id is None or self.ctx.versions.get(server_id, version_id) is None:
            raise RpcError("not_found")
        rows = self.ctx.issues.list_issues([server_id], search=search, version_id=version_id)
        return self._issue_summaries(rows, version=True)

    def _issue_summaries(self, rows, version: bool = False) -> dict[str, Any]:
        show_closed = bool(self.ctx.settings.get("show_closed"))
        names: dict[int, dict[str, Any]] = {}
        out = []
        for r in rows:
            n = names.get(r.server_id)
            if n is None:
                n = names[r.server_id] = {
                    "status": self.ctx.masters.names(r.server_id, "status"),
                    "closed": self.ctx.masters.closed_status_ids(r.server_id),
                    "server": (self.ctx.servers.get(r.server_id) or Server(name="", url="")).name,
                }
            closed = r.status_id in n["closed"]
            if version:
                if closed and not show_closed:
                    continue
            elif not r.monitored and not closed:
                continue  # 「クローズ済みも表示」でも、関係が外れた未完了チケットは出さない
            out.append({
                "serverId": r.server_id, "serverName": n["server"], "issueId": r.issue_id,
                "projectId": r.project_id, "subject": r.subject,
                "status": n["status"].get(r.status_id, ""), "closed": closed,
                # 自分と関係ないチケット(バージョンで見ているもの)のコメントは未読として数えない
                "assignee": r.assigned_to_name, "updatedOn": r.updated_on,
                "unread": r.unread if r.relation_flags else 0,
                "relations": _flags(r.relation_flags), "monitored": r.monitored,
                "lastNoteUser": r.last_note_user, "lastNote": (r.last_note or "")[:120].replace("\n", " "),
            })
        if self.ctx.settings.get("unread_first"):
            out.sort(key=lambda x: 0 if x["unread"] else 1)  # 安定ソートなので更新順は保たれる
        return {"issues": out, "total": len(out)}

    # --- ロードマップ(バージョン) ---------------------------------------------------
    @rpc
    def getVersion(self, serverId: int, versionId: int, markRead: bool = True) -> dict[str, Any]:  # noqa: N802,N803
        """バージョンの概要と変化の記録。markRead なら、変化を見たことにする(ツリーの印を消す)。"""
        server = self.ctx.servers.get(serverId)
        v = self.ctx.versions.get(serverId, versionId)
        if server is None or v is None:
            raise RpcError("not_found")
        read_before = v.read_event_id
        done, total = self.ctx.versions.progress(serverId).get(versionId, (0, 0))
        crumbs = [p.name for p in (self.ctx.projects.get(serverId, pid) for pid in
                  reversed([v.project_id, *self.ctx.projects.ancestors(serverId, v.project_id)])) if p]
        events = [{
            "eventId": e.event_id, "kind": e.kind, "issueId": e.issue_id, "subject": e.subject,
            "oldValue": e.old_value, "newValue": e.new_value, "createdAt": e.created_at,
            "unread": e.event_id > read_before,
        } for e in self.ctx.versions.events(serverId, versionId)]
        if markRead and any(e["unread"] for e in events):
            self.ctx.versions.mark_read(serverId, versionId)
            self.ctx.bus.publish("versionsChanged", serverId=serverId, versionIds=[versionId], source="read")
        return {
            "serverId": serverId, "versionId": versionId, "name": v.name, "status": v.status,
            "dueDate": v.due_date, "description": v.description or "", "projectId": v.project_id,
            "breadcrumb": crumbs, "done": done, "total": total,
            "url": f"{server.url}/versions/{versionId}", "events": events,
        }

    # --- 会話 ---------------------------------------------------------------------
    def _render_ctx(self, server: Server, issue_id: int) -> RenderContext:
        atts = {a.filename.lower(): a.attachment_id for a in self.ctx.issues.attachments(server.server_id, issue_id)}
        row = self.ctx.issues.get(server.server_id, issue_id)
        proj = self.ctx.projects.get(server.server_id, row.project_id) if row else None
        return RenderContext(
            server_id=server.server_id, server_url=server.url, text_format=server.text_format, attachments=atts,
            my_login=server.my_login, project_identifier=proj.identifier if proj else None,
            allow_external_images=bool(self.ctx.settings.get("allow_external_images")),
            user_names=self.ctx.masters.names(server.server_id, "user"),
        )

    def _detail_names(self, server_id: int) -> DetailNames:
        m = self.ctx.masters
        return DetailNames(
            status=m.names(server_id, "status"), user=m.names(server_id, "user"),
            priority=m.names(server_id, "priority"), tracker=m.names(server_id, "tracker"),
            project={p.project_id: p.name for p in self.ctx.projects.list(server_id)},
            custom_field=m.names(server_id, "custom_field"),
        )

    @rpc
    def getConversation(self, serverId: int, issueId: int, limit: int = PAGE_SIZE,  # noqa: N802,N803
                        markRead: bool = True) -> dict[str, Any]:  # noqa: N803
        server = self.ctx.servers.get(serverId)
        row = self.ctx.issues.get(serverId, issueId)
        if server is None or row is None:
            raise RpcError("not_found")
        lang = self.lang
        rctx = self._render_ctx(server, issueId)
        names = self._detail_names(serverId)
        last_read = self.ctx.issues.last_read(serverId, issueId)
        total = self.ctx.issues.journal_count(serverId, issueId)
        journals = self.ctx.issues.journals(serverId, issueId, limit=max(1, min(int(limit), 1000)))
        atts = {a.attachment_id: a for a in self.ctx.issues.attachments(serverId, issueId)}
        items = []
        for j in journals:
            details = [describe_detail(d, names, lang) for d in j.details]
            for d in details:
                a = atts.get(d.get("attachmentId") or -1)
                if a is not None:
                    d["url"] = f"/attachments/{serverId}/{a.attachment_id}"
                    d["isImage"] = (a.content_type or "").startswith("image/") and a.content_type != "image/svg+xml"
                    d["filesize"] = a.filesize
            items.append({
                "journalId": j.journal_id, "userId": j.user_id, "userName": j.user_name, "notes": j.notes,
                "mine": j.user_id is not None and j.user_id == server.my_user_id,
                "private": j.private_notes, "createdOn": j.created_on,
                "html": render(j.notes, rctx) if j.notes else "", "details": details,
                "unread": bool(j.notes) and j.journal_id > last_read and j.user_id != server.my_user_id,
            })
        project = self.ctx.projects.get(serverId, row.project_id)
        crumbs = []
        if project:
            for pid in reversed([project.project_id, *self.ctx.projects.ancestors(serverId, project.project_id)]):
                p = self.ctx.projects.get(serverId, pid)
                if p:
                    crumbs.append(p.name)
        draft, draft_private, draft_failed = self.ctx.issues.get_draft(serverId, issueId)
        m = self.ctx.masters
        result = {
            "serverId": serverId, "serverName": server.name, "issueId": issueId, "subject": row.subject,
            "url": server.issue_url(issueId), "breadcrumb": crumbs,
            "tracker": m.names(serverId, "tracker").get(row.tracker_id or -1, ""),
            "status": m.names(serverId, "status").get(row.status_id or -1, ""),
            "closed": row.status_id in m.closed_status_ids(serverId),
            "priority": m.names(serverId, "priority").get(row.priority_id or -1, ""),
            "assignee": row.assigned_to_name, "author": row.author_name, "authorId": row.author_id,
            "statusId": row.status_id, "priorityId": row.priority_id, "assignedToId": row.assigned_to_id,
            "relations": _flags(row.relation_flags), "watching": bool(row.relation_flags & REL_WATCHING),
            "createdOn": row.created_on, "updatedOn": row.updated_on,
            "descriptionHtml": render(row.description, rctx) if row.description else "",
            "journals": items, "total": total, "hasMore": total > len(journals),
            "lastReadJournalId": last_read, "journalsSynced": bool(row.journals_synced_on),
            "textFormat": server.text_format, "myUserId": server.my_user_id,
            "draft": {"body": draft, "private": draft_private, "failed": draft_failed is not None},
        }
        if markRead:
            self.ctx.issues.mark_opened(serverId, issueId)
            if self.ctx.issues.unread_count(serverId, issueId):
                self.ctx.issues.mark_read(serverId, issueId)
                self.ctx.bus.publish("dataChanged", serverId=serverId, issueIds=[issueId], source="read")
        return result

    @rpc
    def setOpenIssue(self, serverId: int | None, issueId: int | None) -> None:  # noqa: N802,N803
        self.open_issue = (serverId, issueId) if serverId and issueId else None
        if self.sync is not None:
            self.sync.set_open_issue(serverId, issueId)

    @rpc
    def markRead(self, serverId: int, issueId: int) -> None:  # noqa: N802,N803
        self.ctx.issues.mark_read(serverId, issueId)
        self.ctx.bus.publish("dataChanged", serverId=serverId, issueIds=[issueId], source="read")

    @rpc
    def markUnread(self, serverId: int, issueId: int) -> None:  # noqa: N802,N803
        self.ctx.issues.mark_unread(serverId, issueId)
        self.ctx.bus.publish("dataChanged", serverId=serverId, issueIds=[issueId], source="read")

    @rpc
    def markAllRead(self, serverId: int | None = None, projectId: int | None = None) -> None:  # noqa: N802,N803
        sids = [serverId] if serverId else [s.server_id for s in self.ctx.servers.list(enabled_only=True)]
        for sid in sids:
            ids = None
            if projectId:
                ids = {projectId} | self.ctx.projects.descendants(sid, projectId)
            self.ctx.issues.mark_all_read(sid, ids)
            self.ctx.bus.publish("dataChanged", serverId=sid, issueIds=[], source="read")

    # --- 投稿・下書き ------------------------------------------------------------------
    @rpc
    def preview(self, serverId: int, issueId: int, text: str,  # noqa: N803
                pending: list[dict[str, Any]] | None = None) -> str:
        """入力中の本文のプレビュー。pending は送信前の添付 [{filename, id}](画像を表示するため)。"""
        server = self.ctx.servers.get(serverId)
        if server is None:
            raise RpcError("not_found")
        rctx = self._render_ctx(server, issueId)
        rctx.pending = {str(p["filename"]).lower(): f"/pending/{int(p['id'])}" for p in (pending or [])}
        return render(text, rctx)

    @rpc
    def saveDraft(self, serverId: int, issueId: int, body: str, private: bool = False) -> None:  # noqa: N802,N803
        self.ctx.issues.save_draft(serverId, issueId, body, private)

    @rpc
    def postNote(self, serverId: int, issueId: int, body: str = "", private: bool = False,  # noqa: N802,N803
                 changes: dict[str, Any] | None = None, uploads: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """コメント・項目の変更・添付をまとめて送る。結果は noteFinished イベントで届く。

        失敗してもコメントは下書きとして残る(設計書 §6)。
        """
        changes = dict(changes or {})
        if any(not _valid_change(k, v) for k, v in changes.items()):
            raise RpcError("invalid_input", "changes")
        ups = []
        for u in uploads or []:
            if not isinstance(u, dict) or not u.get("token") or not u.get("filename"):
                raise RpcError("invalid_input", "uploads")
            ups.append({"token": str(u["token"]), "filename": str(u["filename"]),
                        "content_type": u.get("contentType") or u.get("content_type")})
        if not body.strip() and not changes and not ups:
            raise RpcError("unprocessable", "empty")
        if body.strip():
            self.ctx.issues.save_draft(serverId, issueId, body, private)
        if self.sync is None or not self.sync.post_note(serverId, issueId, body if body.strip() else "",
                                                        private, changes, ups):
            if body.strip():
                self.ctx.issues.save_draft(serverId, issueId, body, private, failed=True, error="connection_failed")
            raise RpcError("connection_failed")
        return {"accepted": True}

    @rpc
    def editNote(self, serverId: int, issueId: int, journalId: int, notes: str,  # noqa: N802,N803
                 private: bool | None = None) -> None:
        """コメントを書き換える。notes を空にすると削除する(項目の変更も行った更新なら、本文だけが消える)。
        できるかはRedmineが判断する(権限がなければ forbidden)。終わったら、チケットを取り直す。
        """
        if not isinstance(notes, str) or (private is not None and not isinstance(private, bool)):
            raise RpcError("invalid_input")
        if not any(j.journal_id == journalId for j in self.ctx.issues.journals(serverId, issueId)):
            raise RpcError("not_found")
        try:
            self._client(serverId).update_journal(journalId, notes, private)
        except RedmineError as e:
            raise RpcError(e.code.value, e.detail) from e
        self._refetch(serverId, issueId)

    @rpc
    def deleteAttachment(self, serverId: int, issueId: int, attachmentId: int) -> None:  # noqa: N802,N803
        """添付ファイルを削除する(Redmineでは、チケットを編集できる人だけが削除できる)。"""
        a = self.ctx.issues.get_attachment(serverId, attachmentId)
        if a is None or a.issue_id != issueId:
            raise RpcError("not_found")
        try:
            self._client(serverId).delete_attachment(attachmentId)
        except RedmineError as e:
            raise RpcError(e.code.value, e.detail) from e
        self._refetch(serverId, issueId)

    def _refetch(self, server_id: int, issue_id: int) -> None:
        """変更したチケットを、同期スレッドに(コメントごと)取り直させる。結果は dataChanged で画面に届く。"""
        if self.sync is not None:
            self.sync.refetch_issue(server_id, issue_id)

    # --- 編集(設計書 §6.2) -------------------------------------------------------------
    def _client(self, server_id: int):
        client = self.sync.client(server_id) if self.sync is not None else None
        if client is None:
            raise RpcError("connection_failed")
        return client

    def _members_of(self, server_id: int, project_id: int) -> list[dict[str, Any]]:
        key = (server_id, project_id)
        with self._members_lock:
            cached = self._members.get(key)
            if cached and time.monotonic() - cached[0] < MEMBERS_TTL:
                return cached[1]
        out: dict[int, dict[str, Any]] = {}
        for m in self._client(server_id).memberships(project_id):
            ref = m.get("user") or m.get("group")
            if ref and ref.get("id") is not None:
                out[int(ref["id"])] = {"id": int(ref["id"]), "name": ref.get("name", ""), "group": "group" in m}
                self.ctx.masters.remember(server_id, "user", ref.get("id"), ref.get("name"))
        members = sorted(out.values(), key=lambda x: (x["group"], x["name"]))
        with self._members_lock:
            self._members[key] = (time.monotonic(), members)
        return members

    def _my_project_roles(self, server_id: int) -> tuple[bool, dict[int, set[int]]]:
        """自分が管理者か と、プロジェクトごとの自分のロール(グループ経由を含む)。"""
        with self._members_lock:
            cached = self._my_roles.get(server_id)
            if cached and time.monotonic() - cached[0] < PERMISSIONS_TTL:
                return cached[1], cached[2]
        u = self._client(server_id).current_user()
        roles: dict[int, set[int]] = {}
        for m in u.get("memberships") or []:
            pid = (m.get("project") or {}).get("id")
            if pid is not None:
                roles.setdefault(int(pid), set()).update(int(r["id"]) for r in m.get("roles") or [] if "id" in r)
        admin = bool(u.get("admin"))
        with self._members_lock:
            self._my_roles[server_id] = (time.monotonic(), admin, roles)
        return admin, roles

    def _permissions_of(self, server_id: int, role_id: int) -> set[str]:
        key = (server_id, role_id)
        with self._members_lock:
            cached = self._role_perms.get(key)
            if cached and time.monotonic() - cached[0] < PERMISSIONS_TTL:
                return cached[1]
        perms = {str(p) for p in self._client(server_id).role(role_id).get("permissions") or []}
        with self._members_lock:
            self._role_perms[key] = (time.monotonic(), perms)
        return perms

    def _project_permissions(self, server_id: int, project_id: int) -> set[str] | None:
        """このプロジェクトでの自分の権限。管理者なら {"admin"}。分からなければ None。

        分からないのは、メンバーでないプロジェクト(公開プロジェクトの非メンバー、親プロジェクトからの
        メンバーの継承)、古いRedmine、問い合わせに失敗したとき。そのときは書き込みを制限しない
        (できなければ送信時にRedmineが拒否する)。
        """
        try:
            admin, roles = self._my_project_roles(server_id)
            if admin:
                return {"admin"}
            role_ids = roles.get(project_id)
            if not role_ids:
                return None
            perms: set[str] = set()
            for rid in role_ids:
                perms |= self._permissions_of(server_id, rid)
            return perms
        except (RedmineError, RpcError, KeyError, TypeError, ValueError) as e:
            log.info("permissions unavailable for server %s project %s: %s", server_id, project_id, e)
            return None

    def _write_permissions(self, server: Server, row, allowed: list[dict[str, Any]] | None) -> dict[str, Any]:
        """このチケットに書き込めるか(設計書 §6.2)。画面は、できない操作の入力欄を無効にする。

        Redmineの判定(Issue#attributes_editable? / notes_addable?)に合わせる:
        項目(ステータス・担当者・優先度)の変更は edit_issues(自分が作成したチケットなら edit_own_issues)、
        コメントは add_issue_notes、プライベートコメントは set_notes_private が要る。
        終了したプロジェクトのチケットには書き込めない。
        """
        project = self.ctx.projects.get(server.server_id, row.project_id)
        if project is not None and project.status != PROJECT_STATUS_ACTIVE:
            return {"editFields": False, "changeStatus": False, "comment": False, "privateNotes": False,
                    "attach": False, "editNotes": "none", "reason": "project_closed"}
        perms = self._project_permissions(server.server_id, row.project_id)
        if perms is None or "admin" in perms:
            edit = comment = private = True
            edit_notes = "all"
        else:
            own = row.author_id is not None and row.author_id == server.my_user_id
            edit = "edit_issues" in perms or ("edit_own_issues" in perms and own)
            comment = "add_issue_notes" in perms
            private = "set_notes_private" in perms
            # コメントの編集・削除(Redmineの Journal#editable_by? と同じ): すべて / 自分のものだけ / できない
            edit_notes = "all" if "edit_issue_notes" in perms else "own" if "edit_own_issue_notes" in perms else "none"
        # ワークフロー上、今のステータスから移れる先がなければ、ステータスは変えられない(Redmine 5.0 以降)
        transitions = allowed is None or any(int(x["id"]) != row.status_id for x in allowed)
        return {
            "editFields": edit, "changeStatus": edit and transitions, "comment": comment,
            "privateNotes": comment and private, "attach": edit or comment, "editNotes": edit_notes,
            "reason": None if (edit or comment) else "no_permission",
        }

    @rpc
    def getEditOptions(self, serverId: int, issueId: int) -> dict[str, Any]:  # noqa: N802,N803
        """ヘッダで変更できる項目の選択肢と、書き込めるか。ステータスはワークフロー上遷移できるものだけにする。"""
        row = self.ctx.issues.get(serverId, issueId)
        server = self.ctx.servers.get(serverId)
        if row is None or server is None:
            raise RpcError("not_found")
        try:
            allowed = self._client(serverId).allowed_statuses(issueId)
            members = self._members_of(serverId, row.project_id)
        except RedmineError as e:
            raise RpcError(e.code.value, e.detail) from e
        permissions = self._write_permissions(server, row, allowed)
        closed = self.ctx.masters.closed_status_ids(serverId)
        status_names = self.ctx.masters.names(serverId, "status")
        if allowed is None:  # Redmine 5.0 より前: すべてのステータスを出し、だめならサーバーが拒否する
            statuses = [{"id": k, "name": v} for k, v in status_names.items()]
        else:
            statuses = [{"id": int(x["id"]), "name": x.get("name", "")} for x in allowed]
        if row.status_id is not None and all(x["id"] != row.status_id for x in statuses):
            statuses.insert(0, {"id": row.status_id, "name": status_names.get(row.status_id, f"#{row.status_id}")})
        for x in statuses:
            x["closed"] = x["id"] in closed
        if row.assigned_to_id is not None and all(m["id"] != row.assigned_to_id for m in members):
            members = [{"id": row.assigned_to_id, "name": row.assigned_to_name or f"#{row.assigned_to_id}",
                        "group": False}, *members]
        priorities = [{"id": k, "name": v} for k, v in self.ctx.masters.names(serverId, "priority").items()]
        return {
            "statuses": statuses, "statusesExact": allowed is not None,
            "assignees": members, "priorities": priorities, "me": server.my_user_id,
            "current": {"status_id": row.status_id, "assigned_to_id": row.assigned_to_id,
                        "priority_id": row.priority_id},
            "permissions": permissions,
        }

    def _custom_field_defs(self, server_id: int) -> dict[int, dict[str, Any]] | None:
        """カスタムフィールドの定義(ID → 定義)。管理者でなければ取得できないので None。"""
        with self._members_lock:
            cached = self._custom_fields.get(server_id)
        if cached and time.monotonic() - cached[0] < CUSTOM_FIELDS_TTL:
            defs = cached[1]
        else:
            try:
                defs = self._client(server_id).custom_fields()
            except RedmineError as e:
                if e.code not in (ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND):
                    raise
                defs = None
            with self._members_lock:
                self._custom_fields[server_id] = (time.monotonic(), defs)
        return None if defs is None else {int(d["id"]): d for d in defs}

    @rpc
    def getIssueDetails(self, serverId: int, issueId: int) -> dict[str, Any]:  # noqa: N802,N803
        """「詳細」で表示・編集する項目(トラッカー・カテゴリ・対象バージョン・日付・工数・カスタムフィールドなど)の
        今の値と選択肢。書き込めるかは getEditOptions の permissions で判断する。
        """
        row = self.ctx.issues.get(serverId, issueId)
        server = self.ctx.servers.get(serverId)
        if row is None or server is None:
            raise RpcError("not_found")
        client = self._client(serverId)
        try:
            data = client.issue(issueId)
            pid = int(data["project"]["id"])
            project = client.project(pid, include="trackers,issue_categories")
            versions = client.versions(pid)
            members = self._members_of(serverId, pid)
            defs = self._custom_field_defs(serverId)
        except RedmineError as e:
            raise RpcError(e.code.value, e.detail) from e

        def ref(key: str) -> tuple[int | None, str]:
            r = data.get(key) or {}
            return (int(r["id"]) if r.get("id") is not None else None), r.get("name") or ""

        def options(items, current: tuple[int | None, str], none: bool) -> list[dict[str, Any]]:
            out = [{"value": None, "label": ""}] if none else []
            out += [{"value": int(x["id"]), "label": x.get("name", "")} for x in items]
            if current[0] is not None and all(o["value"] != current[0] for o in out):
                out.insert(1 if none else 0, {"value": current[0], "label": current[1] or f"#{current[0]}"})
            return out

        tracker, category, version = ref("tracker"), ref("category"), ref("fixed_version")
        parent = (data.get("parent") or {}).get("id")
        # 対象バージョンに選べるのは、未完了(ロックされていない)のもの
        open_versions = [v for v in versions if (v.get("status") or "open") == "open"]
        fields: list[dict[str, Any]] = [
            {"key": "tracker_id", "type": "select", "value": tracker[0], "display": tracker[1],
             "options": options(project.get("trackers") or [], tracker, False), "editable": True},
            {"key": "category_id", "type": "select", "value": category[0], "display": category[1],
             "options": options(project.get("issue_categories") or [], category, True), "editable": True},
            {"key": "fixed_version_id", "type": "select", "value": version[0], "display": version[1],
             "options": options(open_versions, version, True), "editable": True},
            {"key": "parent_issue_id", "type": "issue", "value": parent,
             "display": f"#{parent}" if parent else "", "editable": True},
            {"key": "start_date", "type": "date", "value": data.get("start_date"),
             "display": data.get("start_date") or "", "editable": True},
            {"key": "due_date", "type": "date", "value": data.get("due_date"),
             "display": data.get("due_date") or "", "editable": True},
            {"key": "done_ratio", "type": "select", "value": int(data.get("done_ratio") or 0),
             "display": f"{int(data.get('done_ratio') or 0)}%",
             "options": [{"value": n, "label": f"{n}%"} for n in range(0, 101, 10)], "editable": True},
            {"key": "estimated_hours", "type": "float", "value": data.get("estimated_hours"),
             "display": "" if data.get("estimated_hours") is None else str(data["estimated_hours"]),
             "editable": True},
        ]
        for cf in data.get("custom_fields") or []:
            fields.append(self._custom_field(cf, (defs or {}).get(int(cf["id"])), defs is not None,
                                             members, versions))
        return {"fields": fields, "customFieldsKnown": defs is not None}

    @staticmethod
    def _custom_field(cf: dict[str, Any], d: dict[str, Any] | None, known: bool,
                      members: list[dict[str, Any]], versions: list[dict[str, Any]]) -> dict[str, Any]:
        """カスタムフィールド1つ。値は文字列(複数選択なら文字列のリスト)で扱う。"""
        multiple = bool(cf.get("multiple") or (d or {}).get("multiple"))
        raw = cf.get("value")
        value: Any = ([str(x) for x in raw] if isinstance(raw, list) else ([str(raw)] if raw not in (None, "") else [])) \
            if multiple else (None if raw in (None, "") else str(raw))
        fmt = (d or {}).get("field_format")
        out: dict[str, Any] = {"key": f"cf:{int(cf['id'])}", "label": cf.get("name", ""), "multiple": multiple,
                               "value": value, "editable": False, "type": "text"}
        opts: list[dict[str, Any]] | None = None
        if fmt in ("list", "enumeration"):
            opts = [{"value": str(pv.get("value")), "label": str(pv.get("label") or pv.get("value"))}
                    for pv in (d or {}).get("possible_values") or []]
        elif fmt == "user":
            opts = [{"value": str(m["id"]), "label": m["name"]} for m in members if not m.get("group")]
        elif fmt == "version":
            opts = [{"value": str(v["id"]), "label": v.get("name", "")} for v in versions]
        elif fmt == "bool":
            opts = [{"value": "1", "label": "1"}, {"value": "0", "label": "0"}]  # 表示名は画面で翻訳する
        if opts is not None:
            out["type"] = "multiselect" if multiple else "select"
            known_values = {o["value"] for o in opts}
            for v in (value if multiple else [value] if value is not None else []):
                if v not in known_values:
                    opts.append({"value": v, "label": v})
            if not multiple:
                opts.insert(0, {"value": None, "label": ""})
            out["options"] = opts
        elif fmt in ("int", "float", "date", "string"):
            out["type"] = {"int": "int", "float": "float", "date": "date", "string": "text"}[fmt]
        labels = {o["value"]: o["label"] for o in opts or []}
        shown = value if multiple else ([value] if value is not None else [])
        out["display"] = ", ".join(labels.get(v, v) for v in shown)
        out["format"] = fmt
        if fmt in CF_EDITABLE_FORMATS:
            out["editable"] = True
        else:
            # 形式が分からない(管理者でない)・長文などは、表示だけにする。理由は画面で示す
            out["reason"] = "custom_field_unknown" if not known or d is None else "custom_field_unsupported"
        return out

    @rpc
    def setWatching(self, serverId: int, issueId: int, watching: bool) -> None:  # noqa: N802,N803
        """自分をウォッチャーに追加・削除する。結果は watchFinished イベントで届く。"""
        if self.sync is None or not self.sync.set_watching(serverId, issueId, bool(watching)):
            raise RpcError("connection_failed")

    # --- 同期・ウィンドウ ----------------------------------------------------------------
    @rpc
    def syncNow(self, serverId: int | None = None, issueId: int | None = None) -> None:  # noqa: N802,N803
        """今すぐ同期する(F5・同期ボタン)。開いているチケットを渡すと、そのコメントも取り直す。"""
        if self.sync is None:
            return
        self.sync.sync_now()
        if serverId and issueId:
            self.setOpenIssue(serverId, issueId)  # 開き直すと、同期スレッドがコメントごと取り直す

    @rpc
    def setActive(self, active: bool) -> None:  # noqa: N802
        self.active = bool(active)
        if self.sync is not None:
            self.sync.set_active(bool(active))

    @rpc
    def openExternal(self, url: str) -> None:  # noqa: N802
        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
            raise RpcError("invalid_url")
        webbrowser.open(url)

    @rpc
    def openUri(self, url: str) -> None:  # noqa: N802
        """本文の自動リンク(link_schemes.json で指定したスキーム、mailto)を、OSで関連付けられたアプリで開く。"""
        if not isinstance(url, str) or any(c in url for c in "\r\n\0") or (
                scheme_of(url) is None and not url.lower().startswith("mailto:")):
            raise RpcError("invalid_url")
        open_with_os(url)

    @rpc
    def openRedmine(self, url: str, external: bool = False) -> str:  # noqa: N802
        """Redmineのページを開く。設定に従ってアプリ内のウィンドウか既定のブラウザで開く(設計書 §5.4)。"""
        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
            raise RpcError("invalid_url")
        if not external and self.ctx.settings.get("redmine_open_in") == "app" and self.window.open_redmine(url):
            return "app"
        webbrowser.open(url)
        return "browser"

    @rpc
    def openAttachment(self, serverId: int, attachmentId: int) -> None:  # noqa: N802,N803
        """画像以外の添付は、Redmineのページをブラウザで開く。"""
        a = self.ctx.issues.get_attachment(serverId, attachmentId)
        server = self.ctx.servers.get(serverId)
        if a is None or server is None:
            raise RpcError("not_found")
        self.openRedmine(f"{server.url}/attachments/{attachmentId}")

    @rpc
    def hideWindow(self) -> None:  # noqa: N802
        self.window.hide()

    @rpc
    def quit(self) -> None:
        self.window.quit()
