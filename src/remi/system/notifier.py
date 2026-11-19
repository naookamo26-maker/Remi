"""デスクトップ通知(設計書 §7.3)。

新着コメント(newComments イベント)を受け取り、通知の設定(グローバル → サーバー → プロジェクト)、
一時停止、表示中のチケットかどうかを確認してから、まとめて1回だけ通知する。

- 通知はチケットごとに1つにする(タグ = サーバーとチケット)。同じチケットの新しい通知は、前の通知を置き換える
- アプリでチケットを読んだら、そのチケットの通知を通知センターから消す
- 通知を押すと remi://open?server=..&issue=.. を開く(プロトコル起動)。トーストが消えた後の通知センターの履歴や、
  アプリを終了した後でも、Remiが起動して(起動していれば前面に出て)そのチケットを開く
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from remi import APP_USER_MODEL_ID
from remi.context import AppContext
from remi.i18n import resolve_language, t

log = logging.getLogger(__name__)

PREVIEW_CHARS = 120
PROTOCOL = "remi"
_OPEN_URL = re.compile(rf"^{PROTOCOL}://open/?\?server=(\d{{1,9}})&issue=(\d{{1,12}})/?$")


def open_url(server_id: int, issue_id: int) -> str:
    """通知を押したときに開くURL(Windowsに登録した remi: プロトコル)。"""
    return f"{PROTOCOL}://open?server={int(server_id)}&issue={int(issue_id)}"


def parse_open_url(url: str) -> tuple[int, int] | None:
    """remi://open?server=1&issue=2 → (1, 2)。ほかの形は受け付けない(だれでも開けるURLなので、厳しく見る)。"""
    m = _OPEN_URL.match((url or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def toast_tag(server_id: int, issue_id: int) -> tuple[str, str]:
    """通知のタグとグループ。同じチケットの通知は置き換え、読んだら消すのに使う(タグは64文字まで)。"""
    return f"{server_id}-{issue_id}", f"s{server_id}"


@dataclass
class Notification:
    title: str
    body: str
    server_id: int
    issue_id: int


class ToastBackend:
    """Windowsのトースト通知。Windows以外ではログに出すだけ。

    通知はアプリと同じ AppUserModelID(Windowsに登録したもの。system/windows.py)で出す。登録していないIDで
    出すと、通知センターの履歴から押してもアプリに届かない。
    """

    def __init__(self) -> None:
        self._toaster = None
        if sys.platform == "win32":
            try:
                from windows_toasts import WindowsToaster

                # WindowsToaster は渡した文字列を AUMID として使う
                self._toaster = WindowsToaster(APP_USER_MODEL_ID)
            except Exception:  # noqa: BLE001
                log.exception("windows-toasts is not available")

    def show(self, n: Notification, on_click: Callable[[], None]) -> None:
        if self._toaster is None:
            # 本文・件名はログに残さない(ログは平文で保存され、問い合わせのときに渡すこともある)
            log.info("notification: server %s issue #%s", n.server_id, n.issue_id)
            return
        from windows_toasts import Toast

        tag, group = toast_tag(n.server_id, n.issue_id)
        # 押したときは remi: プロトコルで開く(履歴から押したとき・アプリの終了後も届く)。表示中のトーストを
        # 押したときは、すぐ開けるよう on_activated でも開く(同じチケットを2回開いても表示は変わらない)
        toast = Toast(text_fields=[n.title, n.body], group=group, launch_action=open_url(n.server_id, n.issue_id),
                      on_activated=lambda _args: on_click())
        toast.tag = tag  # 同じタグの通知は置き換わる(チケットごとに1つにする)
        self._toaster.show_toast(toast)

    def remove(self, server_id: int, issue_id: int) -> None:
        """そのチケットの通知を、通知センターから消す(アプリで読んだとき)。"""
        if self._toaster is None:
            return
        from winrt.windows.ui.notifications import ToastNotificationManager

        tag, group = toast_tag(server_id, issue_id)
        ToastNotificationManager.history.remove_grouped_tag_with_id(tag, group, APP_USER_MODEL_ID)

    def shown_issues(self) -> list[tuple[int, int]]:
        """通知センターに残っている、このアプリの通知の (サーバー, チケット)。"""
        if self._toaster is None:
            return []
        from winrt.windows.ui.notifications import ToastNotificationManager

        out = []
        for item in ToastNotificationManager.history.get_history_with_id(APP_USER_MODEL_ID):
            sid, _, iid = (item.tag or "").partition("-")
            if sid.isdigit() and iid.isdigit():
                out.append((int(sid), int(iid)))
        return out


class Notifier:
    def __init__(self, ctx: AppContext, api, on_open: Callable[[int, int], None],
                 backend: Any | None = None) -> None:
        self.ctx = ctx
        self.api = api  # RemiApi(表示中のチケットとアクティブ状態を知るため)
        self.on_open = on_open
        self.backend = backend or ToastBackend()
        self._snooze_until = 0.0
        self._lock = threading.Lock()
        ctx.bus.on(self._on_event)

    # --- 一時停止(トレイメニュー) ---------------------------------------------------
    def snooze(self, seconds: float) -> None:
        with self._lock:
            self._snooze_until = time.monotonic() + seconds

    def resume(self) -> None:
        with self._lock:
            self._snooze_until = 0.0

    @property
    def snoozed(self) -> bool:
        with self._lock:
            return time.monotonic() < self._snooze_until

    # --- 判定 ---------------------------------------------------------------------
    def _wanted(self, server_id: int, comment: dict[str, Any]) -> bool:
        if not self.ctx.settings.get("notify_enabled") or self.snoozed:
            return False
        server = self.ctx.servers.get(server_id)
        if server is None:
            return False
        project = self.ctx.projects.effective_setting(server_id, comment["project_id"], "notify")
        enabled = server.notify_enabled if project is None else project
        if not enabled:
            return False
        # 一覧に出さないチケット(表示するチケット・絞り込みの選択から外れたもの)は通知しない。
        # ツリー・一覧・トレイの未読数と同じ判定にする
        row = self.ctx.issues.get(server_id, comment["issue_id"])
        if row is not None and not row.relation_flags:
            return False  # 自分と関係ない(バージョンで見ている)チケット
        if row is not None and not self.ctx.relation_scope().matches(server_id, row.project_id, row.relation_flags):
            return False
        # 表示中(ウィンドウがアクティブで開いている)のチケットは通知しない
        open_ = getattr(self.api, "open_issue", None)
        if getattr(self.api, "active", False) and open_ == (server_id, comment["issue_id"]):
            return False
        return True

    def build(self, server_id: int, comments: list[dict[str, Any]]) -> Notification | None:
        wanted = [c for c in comments if self._wanted(server_id, c)]
        if not wanted:
            return None
        lang = resolve_language(self.ctx.settings.get("language"))
        first = wanted[-1]  # いちばん新しいもの
        preview = " ".join((first.get("notes") or "").split())
        if len(preview) > PREVIEW_CHARS:
            preview = preview[:PREVIEW_CHARS] + "…"
        title = t("notify.title", lang, id=first["issue_id"], subject=first.get("subject", ""))
        body = t("notify.body", lang, user=first.get("user_name", ""), notes=preview)
        others = len(wanted) - 1
        if others:
            body += "\n" + t("notify.others", lang, count=others)
        return Notification(title, body, server_id, first["issue_id"])

    # --- 読んだチケットの通知を消す ---------------------------------------------------------
    def _clear_read(self, server_id: int, issue_ids: list[int]) -> None:
        """未読がなくなったチケットの通知を、通知センターから消す。issue_ids が空なら(すべて既読にした)、
        通知センターに残っているこのサーバーの通知をすべて確かめる。"""
        remove = getattr(self.backend, "remove", None)
        if remove is None:
            return
        if not issue_ids:
            shown = getattr(self.backend, "shown_issues", None)
            issue_ids = [iid for sid, iid in (shown() if shown else []) if sid == server_id]
        for iid in issue_ids:
            if self.ctx.issues.unread_count(server_id, iid) == 0:
                remove(server_id, iid)

    def _on_event(self, event: dict[str, Any]) -> None:
        if event.get("type") == "dataChanged" and event.get("source") == "read":
            try:
                self._clear_read(event["serverId"], list(event.get("issueIds") or []))
            except Exception:  # noqa: BLE001 - 消せなくてもアプリは止めない
                log.exception("failed to remove notifications")
            return
        if event.get("type") != "newComments":
            return
        n = self.build(event["serverId"], event.get("comments") or [])
        if n is None:
            return
        try:
            self.backend.show(n, lambda: self.on_open(n.server_id, n.issue_id))
        except Exception:  # noqa: BLE001 - 通知に失敗してもアプリは止めない
            log.exception("failed to show a notification")
