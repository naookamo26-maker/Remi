"""画面(JavaScript)から呼べるAPI(設計書 §2.6)。

画面から呼べるのは @rpc を付けたメソッドだけ。引数と戻り値はJSONで表せる値にする。
エラーは RpcError(コード) で返し、文言は画面側で翻訳する。
"""

from __future__ import annotations

import logging
import webbrowser
from collections.abc import Callable
from typing import Any, Protocol

from remi import __version__
from remi.api.client import RedmineClient, normalize_url
from remi.api.errors import RedmineError
from remi.api.probe import probe_server
from remi.context import AppContext
from remi.i18n import SUPPORTED_LANGUAGES, catalog, resolve_language
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
}


class RpcError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class WindowController(Protocol):
    def show(self) -> None: ...
    def hide(self) -> None: ...
    def quit(self) -> None: ...
    def apply_theme(self, theme: str) -> None: ...


class NullWindow:
    """ウィンドウがない場合(ブラウザでの開発モード、テスト)。"""

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
        if data.get("userId") is None and existing is None:
            raise RpcError("not_tested")

        s = existing or Server(name=name, url=url, sort_order=len(self.ctx.servers.list()))
        url_changed = existing is not None and existing.url != url
        s.name, s.url, s.text_format, s.poll_interval = name, url, fmt, poll
        s.notify_enabled = bool(data.get("notifyEnabled", True))
        s.enabled = bool(data.get("enabled", True))
        s.proxy_mode, s.proxy_url = proxy_mode, (data.get("proxyUrl") or None)
        if data.get("userId") is not None:
            s.my_user_id = int(data["userId"])
            s.my_login = data.get("login")
            s.my_name = data.get("userName")
            s.my_group_ids = [int(x) for x in data.get("groupIds", [])]
        if url_changed:
            # 別のサーバーになった: キャッシュを捨てて初回同期からやり直す
            s.initial_sync_done, s.last_sync_cursor, s.last_error, s.last_synced_at = False, None, None, None
        self.ctx.servers.save(s, api_key=api_key)
        if url_changed:
            with self.ctx.db.tx() as c:
                for t_ in ("issues", "journals", "attachments", "read_state", "drafts", "projects", "masters"):
                    c.execute(f"DELETE FROM {t_} WHERE server_id = ?", (s.server_id,))
        self._servers_changed()
        return self._server_dict(s)

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
                   unreadOnly: bool = False, search: str = "") -> dict[str, Any]:  # noqa: N803
        server_ids = [serverId] if serverId else [s.server_id for s in self.ctx.servers.list(enabled_only=True)]
        project_ids = None
        if serverId and projectId:
            project_ids = {projectId} | self.ctx.projects.descendants(serverId, projectId)
        show_closed = bool(self.ctx.settings.get("show_closed"))
        rows = self.ctx.issues.list_issues(server_ids, project_ids, include_unmonitored=show_closed,
                                           unread_only=unreadOnly, search=search)
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
    def preview(self, serverId: int, issueId: int, text: str) -> str:  # noqa: N803
        server = self.ctx.servers.get(serverId)
        if server is None:
            raise RpcError("not_found")
        return render(text, self._render_ctx(server, issueId))

    @rpc
    def saveDraft(self, serverId: int, issueId: int, body: str, private: bool = False) -> None:  # noqa: N802,N803
        self.ctx.issues.save_draft(serverId, issueId, body, private)

    @rpc
    def postNote(self, serverId: int, issueId: int, body: str, private: bool = False) -> dict[str, Any]:  # noqa: N802,N803
        """コメントを送信する。結果は noteFinished イベントで届く。失敗しても下書きは残る。"""
        if not body.strip():
            raise RpcError("unprocessable", "empty")
        self.ctx.issues.save_draft(serverId, issueId, body, private)
        if self.sync is None or not self.sync.post_note(serverId, issueId, body, private):
            self.ctx.issues.save_draft(serverId, issueId, body, private, failed=True, error="connection_failed")
            raise RpcError("connection_failed")
        return {"accepted": True}

    # --- 同期・ウィンドウ ----------------------------------------------------------------
    @rpc
    def syncNow(self) -> None:  # noqa: N802
        if self.sync is not None:
            self.sync.sync_now()

    @rpc
    def setActive(self, active: bool) -> None:  # noqa: N802
        if self.sync is not None:
            self.sync.set_active(bool(active))

    @rpc
    def openExternal(self, url: str) -> None:  # noqa: N802
        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
            raise RpcError("invalid_url")
        webbrowser.open(url)

    @rpc
    def hideWindow(self) -> None:  # noqa: N802
        self.window.hide()

    @rpc
    def quit(self) -> None:
        self.window.quit()
