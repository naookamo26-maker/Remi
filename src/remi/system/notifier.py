"""デスクトップ通知(設計書 §7.3)。

新着コメント(newComments イベント)を受け取り、通知の設定(グローバル → サーバー → プロジェクト)、
一時停止、表示中のチケットかどうかを確認してから、まとめて1回だけ通知する。
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from remi import APP_NAME
from remi.context import AppContext
from remi.i18n import resolve_language, t

log = logging.getLogger(__name__)

PREVIEW_CHARS = 120


@dataclass
class Notification:
    title: str
    body: str
    server_id: int
    issue_id: int


class ToastBackend:
    """Windowsのトースト通知。Windows以外ではログに出すだけ。"""

    def __init__(self) -> None:
        self._toaster = None
        if sys.platform == "win32":
            try:
                from windows_toasts import WindowsToaster

                self._toaster = WindowsToaster(APP_NAME)
            except Exception:  # noqa: BLE001
                log.exception("windows-toasts is not available")

    def show(self, n: Notification, on_click: Callable[[], None]) -> None:
        if self._toaster is None:
            log.info("notification: %s / %s", n.title, n.body)
            return
        from windows_toasts import Toast

        toast = Toast(text_fields=[n.title, n.body], on_activated=lambda _args: on_click())
        self._toaster.show_toast(toast)


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

    def _on_event(self, event: dict[str, Any]) -> None:
        if event.get("type") != "newComments":
            return
        n = self.build(event["serverId"], event.get("comments") or [])
        if n is None:
            return
        try:
            self.backend.show(n, lambda: self.on_open(n.server_id, n.issue_id))
        except Exception:  # noqa: BLE001 - 通知に失敗してもアプリは止めない
            log.exception("failed to show a notification")
