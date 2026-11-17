"""トレイ常駐(設計書 §8, §9.3)。pystray で表示し、アイコンは Pillow で描く。"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from remi import APP_NAME
from remi.context import AppContext
from remi.i18n import resolve_language, t
from remi.system import windows

log = logging.getLogger(__name__)

SNOOZE_SECONDS = 3600


def make_icon(unread: bool, dark_taskbar: bool, size: int = 64):
    """トレイアイコンの画像。タスクバーの色に合わせ、未読があれば赤い点を付ける。"""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = (98, 168, 240, 255) if dark_taskbar else (15, 108, 189, 255)
    fg = (11, 22, 32, 255) if dark_taskbar else (255, 255, 255, 255)
    pad = size // 16
    d.rounded_rectangle((pad, pad, size - pad, size - pad), radius=size // 5, fill=bg)
    try:
        font = ImageFont.load_default(size=int(size * 0.62))
    except TypeError:  # 古いPillow
        font = ImageFont.load_default()
    d.text((size / 2, size / 2), "R", fill=fg, font=font, anchor="mm")
    if unread:
        r = size // 5
        cx, cy = size - r - 1, r + 1
        d.ellipse((cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2), fill=(0, 0, 0, 0))
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(207, 34, 46, 255))
    return img


class Tray:
    def __init__(self, ctx: AppContext, *, on_open: Callable[[], None], on_settings: Callable[[], None],
                 on_sync: Callable[[], None], on_quit: Callable[[], None], notifier=None) -> None:
        self.ctx = ctx
        self.on_open = on_open
        self.on_settings = on_settings
        self.on_sync = on_sync
        self.on_quit = on_quit
        self.notifier = notifier
        self.icon = None
        self._unread = 0
        self._lock = threading.Lock()
        ctx.bus.on(self._on_event)

    @property
    def lang(self) -> str:
        return resolve_language(self.ctx.settings.get("language"))

    def unread_total(self) -> int:
        return sum(sum(self.ctx.issues.unread_by_project(s.server_id).values())
                   for s in self.ctx.servers.list(enabled_only=True))

    def status_text(self) -> str:
        times = [s.last_synced_at for s in self.ctx.servers.list(enabled_only=True) if s.last_synced_at]
        parts = [t("tray.unread", self.lang, count=self._unread)]
        if times:
            from datetime import datetime

            try:
                local = datetime.fromisoformat(max(times).replace("Z", "+00:00")).astimezone()
                parts.append(t("tray.lastSynced", self.lang, time=local.strftime("%H:%M")))
            except ValueError:
                pass
        return " · ".join(parts)

    def menu_items(self) -> list[tuple[str, Any]]:
        """(文言, 処理) の一覧。処理が None の行は表示だけ。区切りは ("-", None)。テストしやすいように分けてある。"""
        lang = self.lang
        snoozed = bool(self.notifier and self.notifier.snoozed)
        return [
            (t("tray.open", lang), self.on_open),
            (self.status_text(), None),
            ("-", None),
            (t("tray.syncNow", lang), self.on_sync),
            (t("tray.resume", lang) if snoozed else t("tray.snooze", lang), self.toggle_snooze),
            (t("tray.settings", lang), self.on_settings),
            ("-", None),
            (t("tray.quit", lang), self.quit),
        ]

    def toggle_snooze(self) -> None:
        if self.notifier is None:
            return
        if self.notifier.snoozed:
            self.notifier.resume()
        else:
            self.notifier.snooze(SNOOZE_SECONDS)
        self._refresh_menu()

    def quit(self) -> None:
        if self.icon is not None:
            self.icon.stop()
        self.on_quit()

    def start(self) -> bool:
        try:
            import pystray
        except Exception:  # noqa: BLE001
            log.exception("pystray is not available")
            return False

        def menu():
            items = []
            for i, (label, action) in enumerate(self.menu_items()):
                if label == "-":
                    items.append(pystray.Menu.SEPARATOR)
                elif action is None:
                    items.append(pystray.MenuItem(label, None, enabled=False))
                else:
                    items.append(pystray.MenuItem(label, (lambda a: lambda _i, _m: a())(action), default=(i == 0)))
            return items

        self._unread = self.unread_total()
        try:
            self.icon = pystray.Icon(APP_NAME, make_icon(self._unread > 0, windows.taskbar_uses_dark()),
                                     self.tooltip(), menu=pystray.Menu(menu))
            self.icon.run_detached()
        except Exception:  # noqa: BLE001
            log.exception("could not start the tray icon")
            self.icon = None
            return False
        return True

    def tooltip(self) -> str:
        return f"{APP_NAME} — {t('tray.unread', self.lang, count=self._unread)}"

    def _refresh_menu(self) -> None:
        if self.icon is not None:
            try:
                self.icon.update_menu()
            except Exception:  # noqa: BLE001
                log.debug("update_menu failed", exc_info=True)

    def refresh(self) -> None:
        with self._lock:
            unread = self.unread_total()
            changed = (unread > 0) != (self._unread > 0)
            self._unread = unread
        if self.icon is None:
            return
        if changed:
            self.icon.icon = make_icon(unread > 0, windows.taskbar_uses_dark())
        self.icon.title = self.tooltip()
        self._refresh_menu()

    def _on_event(self, event: dict[str, Any]) -> None:
        if event.get("type") in ("dataChanged", "syncState", "serversChanged", "settingsChanged"):
            try:
                self.refresh()
            except Exception:  # noqa: BLE001
                log.debug("tray refresh failed", exc_info=True)
