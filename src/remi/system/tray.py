"""トレイ常駐(設計書 §8, §9.3)。pystray で表示し、アイコンは Pillow で描く。"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable
from typing import Any

from remi import APP_NAME
from remi.context import AppContext
from remi.i18n import resolve_language, t
from remi.store.issues import shown_relations
from remi.system import windows

log = logging.getLogger(__name__)

SNOOZE_SECONDS = 3600


# アイコンの形(100x100 の座標。web/icon.svg と同じ数値を使うこと)
# 角の丸い四角のグラデーションに、白い R と e を重ねる。e の下端は R の下端にそろえ、
# e の周りは R を円形に切り抜いてすき間を空ける。R の縦線が太く左が重く見えるので、全体を少し右へ寄せている
BG_BOX = (4, 4, 96, 96)
BG_RADIUS = 22
# R の外形。丸みから脚へ切り替わる角は e の切り抜きの円の内側を通るように近道させている
# (角が円の外へわずかに出て、小さな歯が残るのを防ぐ。見える部分の形は元の R と同じ)
R_PATH = ("M27 17H56C70 17 79 26.5 79 39C79 47.52 74.57 54.03 67.53 57.45L67.04 61.62L81 83H62L48.5 60.5H44V83H27Z "
          "M44 31V47H55C60.5 47 63 44 63 39C63 34 60.5 31 55 31Z")
R_TRANSFORM = (0.9174, -8.77, 4.03)  # (倍率, x の移動, y の移動)
E_CENTER = (72.16, 62.54)
E_RADIUS = 13.25
E_STROKE = 9.17
E_END_DEG = 42      # e の書き終わり(右下)の角度。横棒の右端から上・左・下を回ってここまで描く
E_CUT_RADIUS = 20.4  # R を切り抜く円の半径(e の外側 + すき間)
GRADIENT = ((0x0B, 0x5A, 0x9E), (0x25, 0xB5, 0xE0))  # 左上 → 右下


def _path_polygons(d: str, steps: int = 16) -> list[list[tuple[float, float]]]:
    """M / H / V / L / C / Z(絶対座標)だけからなるSVGパスを、多角形のリストにする。"""
    import re

    tokens = re.findall(r"[MHVLCZ]|-?\d*\.?\d+", d)
    polys: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    x = y = 0.0
    i = 0
    cmd = ""
    while i < len(tokens):
        if tokens[i] in "MHVLCZ":
            cmd = tokens[i]
            i += 1
            if cmd == "Z":
                if cur:
                    polys.append(cur)
                cur = []
                continue
        nums = lambda n: [float(v) for v in tokens[i:i + n]]  # noqa: E731
        if cmd == "M":
            if cur:
                polys.append(cur)
            x, y = nums(2)
            cur = [(x, y)]
            i += 2
            cmd = "L"
        elif cmd == "L":
            x, y = nums(2)
            cur.append((x, y))
            i += 2
        elif cmd == "H":
            (x,) = nums(1)
            cur.append((x, y))
            i += 1
        elif cmd == "V":
            (y,) = nums(1)
            cur.append((x, y))
            i += 1
        elif cmd == "C":
            x1, y1, x2, y2, x3, y3 = nums(6)
            x0, y0 = x, y
            for k in range(1, steps + 1):
                t_ = k / steps
                u = 1 - t_
                cur.append((u ** 3 * x0 + 3 * u * u * t_ * x1 + 3 * u * t_ * t_ * x2 + t_ ** 3 * x3,
                            u ** 3 * y0 + 3 * u * u * t_ * y1 + 3 * u * t_ * t_ * y2 + t_ ** 3 * y3))
            x, y = x3, y3
            i += 6
        else:
            i += 1
    if cur:
        polys.append(cur)
    return polys


def make_icon(unread: bool, dark_taskbar: bool, size: int = 64):
    """アイコンの画像: 角の丸い青いグラデーションに、白い「R」と「e」。未読があれば右上に赤い点を付ける。

    dark_taskbar は互換のために残している(背景の四角はどちらの背景でも見えるので、色は変えない)。
    """
    from PIL import Image, ImageDraw

    scale = 8 if size <= 64 else 4  # 大きく描いてから縮小し、縁を滑らかにする
    big = size * scale
    f = big / 100

    def pts(poly, t=(1.0, 0.0, 0.0)):
        k, dx, dy = t
        return [((px * k + dx) * f, (py * k + dy) * f) for px, py in poly]

    # 角の丸い四角に切り抜いたグラデーション
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle([v * f for v in BG_BOX], radius=BG_RADIUS * f, fill=255)
    grad = Image.new("RGBA", (big, big))
    (r0, g0, b0), (r1, g1, b1) = GRADIENT
    gdraw = ImageDraw.Draw(grad)
    for i in range(0, 2 * big, 2):  # 左上から右下への斜めのグラデーション
        t_ = i / (2 * big)
        color = (int(r0 + (r1 - r0) * t_), int(g0 + (g1 - g0) * t_), int(b0 + (b1 - b0) * t_), 255)
        gdraw.line([(i, 0), (0, i)], fill=color, width=3)
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)

    # R(外形を白で塗り、穴を背景の色に戻す)。e の周りは円形に切り抜く
    outer, hole = _path_polygons(R_PATH)
    rmask = Image.new("L", (big, big), 0)
    rd = ImageDraw.Draw(rmask)
    rd.polygon(pts(outer, R_TRANSFORM), fill=255)
    rd.polygon(pts(hole, R_TRANSFORM), fill=0)
    (ecx, ecy), cut = E_CENTER, E_CUT_RADIUS
    rd.ellipse(((ecx - cut) * f, (ecy - cut) * f, (ecx + cut) * f, (ecy + cut) * f), fill=0)
    # e: 円弧(書き終わりから時計回りに右端まで = 下・左・上を回る)と横棒。線の両端は丸める
    ex, ey, er, half = ecx * f, ecy * f, E_RADIUS * f, E_STROKE / 2 * f
    rd.arc((ex - er - half, ey - er - half, ex + er + half, ey + er + half), E_END_DEG, 360,
           fill=255, width=round(E_STROKE * f))
    rd.rectangle((ex - er, ey - half, ex + er, ey + half), fill=255)
    end = math.radians(E_END_DEG)
    for px, py in ((ex - er, ey), (ex + er * math.cos(end), ey + er * math.sin(end))):
        rd.ellipse((px - half, py - half, px + half, py + half), fill=255)
    img.paste(Image.new("RGBA", (big, big), (255, 255, 255, 255)), (0, 0), rmask)

    if unread:
        d = ImageDraw.Draw(img)
        r = big // 5
        cx, cy = big - r - scale, r + scale
        d.ellipse((cx - r - 2 * scale, cy - r - 2 * scale, cx + r + 2 * scale, cy + r + 2 * scale), fill=(255, 255, 255, 255))
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(207, 34, 46, 255))
    return img.resize((size, size), Image.Resampling.LANCZOS)


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
        # ツリー・一覧と同じく、一覧に出すチケット(表示するチケットと絞り込みの選択)の未読だけを数える
        relations = shown_relations(self.ctx.settings.get("visible_relations"), self.ctx.settings.get("relation_filter"))
        return sum(sum(self.ctx.issues.unread_by_project(s.server_id, relations).values())
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
