"""画面(JavaScript)から呼べるAPI(設計書 §2.6)。

画面から呼べるのは @rpc を付けたメソッドだけ。引数と戻り値はJSONで表せる値にする。
エラーは RpcError(コード) で返し、文言は画面側で翻訳する。
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
import webbrowser
from collections.abc import Callable
from typing import Any, Protocol

from remi import __version__
from remi.api.client import RedmineClient, normalize_url
from remi.api.errors import RedmineError
from remi.api.probe import probe_server
from remi.context import AppContext
from remi.i18n import SUPPORTED_LANGUAGES, catalog, resolve_language
from remi.paths import attachment_cache_dir
from remi.render.details import DetailNames, describe_detail
from remi.render.markup import TEXT_FORMATS, RenderContext, render
from remi.server.tree import build_tree
from remi.store.issues import REL_ASSIGNED, REL_AUTHORED, REL_GROUP, REL_WATCHING
from remi.store.projects import SETTING_KEYS
from remi.store.servers import POLL_INTERVALS, Server

log = logging.getLogger(__name__)

PAGE_SIZE = 50
# 画面から変更できるグローバル設定と、その値の検証
SETTABLE: dict[str, Callable[[Any], bool]] = {
    "theme": lambda v: v in ("system", "light", "dark"),
    "language": lambda v: v == "system" or v in SUPPORTED_LANGUAGES,
    "notify_enabled": lambda v: isinstance(v, bool),
    "autostart": lambda v: isinstance(v, bool),
    "show_all_projects": lambda v: isinstance(v, bool),
    "show_closed": lambda v: isinstance(v, bool),
    "unread_first": lambda v: isinstance(v, bool),
    "closed_retention_days": lambda v: isinstance(v, int) and 0 <= v <= 365,
    "allow_external_images": lambda v: isinstance(v, bool),
    "mine_side": lambda v: v in ("right", "left"),
    "pane_sizes": lambda v: isinstance(v, list) and len(v) <= 3 and all(isinstance(x, int | float) for x in v),
    "tree_collapsed": lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v),
    "redmine_open_in": lambda v: v in ("app", "browser"),
    "relation_filter": lambda v: v in RELATION_FILTERS,
}
# 一覧の絞り込み(設計書 §5.1)
RELATION_FILTERS = {"all": 0, "assigned": REL_ASSIGNED, "watching": REL_WATCHING, "authored": REL_AUTHORED}
# 画面から変更できる項目(設計書 §6.2)
EDITABLE = ("status_id", "assigned_to_id", "priority_id")
MEMBERS_TTL = 600
# サーバーから取得したデータ(認証ユーザーの権限で見えるもの)と、その利用者の状態を持つテーブル。
# 接続先のURLや認証ユーザーが変わったら捨てる。project_settings は利用者の好みなので残す。
SERVER_CACHE_TABLES = ("issues", "journals", "attachments", "read_state", "drafts", "projects", "masters")
# 初期化の前に同期スレッドを止めるときの待ち時間。通信中でも読み取りのタイムアウト(30秒)で終わる
SYNC_STOP_TIMEOUT = 40.0


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
        self._members_lock = threading.Lock()

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
            # 初期化の後に、動いている同期スレッドが前のデータを書き戻さないよう、先に止める
            self.sync.stop_all(SYNC_STOP_TIMEOUT)
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
        p = self.ctx.projects.get(serverId, projectId)
        out["url"] = f"{server.url}/projects/{p.identifier or p.project_id}" if server and p else None
        return out

    @rpc
    def setProjectSetting(self, serverId: int, projectId: int, key: str, value: bool | None) -> dict[str, Any]:  # noqa: N802,N803
        if key not in SETTING_KEYS or value not in (None, True, False):
            raise RpcError("invalid_setting", key)
        self.ctx.projects.set_setting(serverId, projectId, key, value)
        if key == "watch_authored" and self.sync is not None:
            # 作成したチケットを取り直すため、初回同期からやり直す
            self.ctx.servers.update_fields(serverId, initial_sync_done=False)
            self.sync.restart()
        self.ctx.bus.publish("projectSettingsChanged", serverId=serverId)
        return self.getProjectSettings(serverId, projectId)

    # --- 一覧 ---------------------------------------------------------------------
    @rpc
    def listIssues(self, serverId: int | None = None, projectId: int | None = None,  # noqa: N802,N803
                   unreadOnly: bool = False, search: str = "", relation: str = "all") -> dict[str, Any]:  # noqa: N803
        server_ids = [serverId] if serverId else [s.server_id for s in self.ctx.servers.list(enabled_only=True)]
        project_ids = None
        if serverId and projectId:
            project_ids = {projectId} | self.ctx.projects.descendants(serverId, projectId)
        show_closed = bool(self.ctx.settings.get("show_closed"))
        rows = self.ctx.issues.list_issues(server_ids, project_ids, include_unmonitored=show_closed,
                                           unread_only=unreadOnly, search=search)
        if relation not in RELATION_FILTERS:
            raise RpcError("invalid_input", "relation")
        want = RELATION_FILTERS[relation]
        names: dict[int, dict[str, Any]] = {}
        out = []
        for r in rows:
            if want and not r.relation_flags & want:
                continue
            n = names.get(r.server_id)
            if n is None:
                n = names[r.server_id] = {
                    "status": self.ctx.masters.names(r.server_id, "status"),
                    "closed": self.ctx.masters.closed_status_ids(r.server_id),
                    "server": (self.ctx.servers.get(r.server_id) or Server(name="", url="")).name,
                }
            closed = r.status_id in n["closed"]
            if not r.monitored and not closed:
                continue  # 「クローズ済みも表示」でも、関係が外れた未完了チケットは出さない
            out.append({
                "serverId": r.server_id, "serverName": n["server"], "issueId": r.issue_id,
                "projectId": r.project_id, "subject": r.subject,
                "status": n["status"].get(r.status_id, ""), "closed": closed,
                "assignee": r.assigned_to_name, "updatedOn": r.updated_on, "unread": r.unread,
                "relations": _flags(r.relation_flags), "monitored": r.monitored,
                "lastNoteUser": r.last_note_user, "lastNote": (r.last_note or "")[:120].replace("\n", " "),
            })
        if self.ctx.settings.get("unread_first"):
            out.sort(key=lambda x: 0 if x["unread"] else 1)  # 安定ソートなので更新順は保たれる
        return {"issues": out, "total": len(out)}

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
                "journalId": j.journal_id, "userId": j.user_id, "userName": j.user_name,
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
        if any(k not in EDITABLE for k in changes):
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

    @rpc
    def getEditOptions(self, serverId: int, issueId: int) -> dict[str, Any]:  # noqa: N802,N803
        """ヘッダで変更できる項目の選択肢。ステータスはワークフロー上遷移できるものだけにする。"""
        row = self.ctx.issues.get(serverId, issueId)
        server = self.ctx.servers.get(serverId)
        if row is None or server is None:
            raise RpcError("not_found")
        try:
            allowed = self._client(serverId).allowed_statuses(issueId)
            members = self._members_of(serverId, row.project_id)
        except RedmineError as e:
            raise RpcError(e.code.value, e.detail) from e
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
        }

    @rpc
    def setWatching(self, serverId: int, issueId: int, watching: bool) -> None:  # noqa: N802,N803
        """自分をウォッチャーに追加・削除する。結果は watchFinished イベントで届く。"""
        if self.sync is None or not self.sync.set_watching(serverId, issueId, bool(watching)):
            raise RpcError("connection_failed")

    # --- 同期・ウィンドウ ----------------------------------------------------------------
    @rpc
    def syncNow(self) -> None:  # noqa: N802
        if self.sync is not None:
            self.sync.sync_now()

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
