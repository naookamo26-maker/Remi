"""アプリの組み立てと起動。"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import webbrowser

from remi import APP_NAME, APP_USER_MODEL_ID, __version__
from remi.context import AppContext
from remi.i18n import resolve_language, t
from remi.logging_setup import setup_logging
from remi.paths import data_dir, db_path
from remi.server.api import RemiApi
from remi.server.http import LocalServer
from remi.store.db import SchemaTooNewError, open_database
from remi.sync.worker import SyncManager
from remi.system import windows
from remi.system.notifier import Notifier
from remi.system.single_instance import SingleInstance
from remi.system.tray import Tray

log = logging.getLogger(__name__)


class WebviewWindow:
    """pywebview(WebView2)のウィンドウ(設計書 §2.6, §8)。"""

    def __init__(self, ctx: AppContext, sync: SyncManager, start_hidden: bool = False) -> None:
        self.ctx = ctx
        self.sync = sync
        self.start_hidden = start_hidden
        self.window = None
        self.redmine_window = None  # Redmineのページを開くウィンドウ(設計書 §5.4)
        self.minimize_to_tray = False  # トレイが使えるときに True にする
        self._quitting = False

    def run(self, url: str) -> None:
        import webview

        self.window = webview.create_window(
            f"{APP_NAME} v{__version__}", url, width=1280, height=800, min_size=(900, 560), hidden=self.start_hidden,
            background_color="#1E1F22" if self._dark() else "#FFFFFF", text_select=True,
        )
        self.window.events.closing += self._on_closing
        self.window.events.shown += lambda: self.apply_theme(self.ctx.settings.get("theme"))
        gui = "edgechromium" if sys.platform == "win32" else None
        # Redmineのページのログイン状態を残すため、WebView2のデータを保存する
        storage = data_dir() / "webview"
        storage.mkdir(parents=True, exist_ok=True)
        webview.start(gui=gui, private_mode=False, storage_path=str(storage), debug=False, icon=self._icon_path())

    @staticmethod
    def _icon_path() -> str | None:
        """タイトルバーのアイコン。exe化していない(python -m remi で起動した)ときもRemiのアイコンにする。"""
        try:
            from remi.system.tray import make_icon

            path = data_dir() / "remi.ico"
            make_icon(unread=False, dark_taskbar=False, size=256).save(
                path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
            return str(path)
        except Exception:  # noqa: BLE001 - アイコンがなくても起動はできる
            log.debug("could not write the window icon", exc_info=True)
            return None

    def _dark(self) -> bool:
        theme = self.ctx.settings.get("theme")
        return theme == "dark" or (theme == "system" and windows.apps_use_dark())

    def _on_closing(self) -> bool:
        if self._quitting or not self.minimize_to_tray:
            return True
        self.hide()
        return False  # 閉じる操作を取り消して、トレイへ格納する

    # --- WindowController ---
    def show(self) -> None:
        if self.window is not None:
            self.window.show()
            self.window.restore()

    def hide(self) -> None:
        if self.window is not None:
            self.window.hide()

    def quit(self) -> None:
        self._quitting = True
        if self.redmine_window is not None:
            try:
                self.redmine_window.destroy()
            except Exception:  # noqa: BLE001
                pass
        if self.window is not None:
            self.window.destroy()

    def open_redmine(self, url: str) -> bool:
        """Redmineのページをアプリ内のウィンドウで開く。2回目以降は同じウィンドウを使い回す。"""
        import webview

        try:
            if self.redmine_window is not None:
                self.redmine_window.load_url(url)
                self.redmine_window.show()
                self.redmine_window.restore()
                return True
            w = webview.create_window(f"{APP_NAME} — Redmine", url, width=1100, height=800, min_size=(640, 480),
                                      text_select=True)

            def closed() -> None:
                self.redmine_window = None

            w.events.closed += closed
            self.redmine_window = w
            return True
        except Exception:  # noqa: BLE001 - 開けなければ既定のブラウザで開く
            log.exception("could not open the Redmine window")
            self.redmine_window = None
            return False

    def apply_theme(self, theme: str) -> None:
        if self.window is None or sys.platform != "win32":
            return
        try:
            hwnd = int(self.window.native.Handle.ToInt64())
        except Exception:  # noqa: BLE001
            return
        windows.set_dark_title_bar(hwnd, self._dark())


class BrowserWindow:
    """開発モード: 既定のブラウザで画面を開く。"""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.url = ""

    def run(self, url: str) -> None:
        self.url = url
        webbrowser.open(url)
        print(f"Remi is running at {url}  (Ctrl+C to quit)", flush=True)
        try:
            while not self.done.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass

    def show(self) -> None:
        webbrowser.open(self.url)

    def open_redmine(self, url: str) -> bool:
        return False  # 開発モードでは既定のブラウザで開く

    def hide(self) -> None:
        pass

    def quit(self) -> None:
        self.done.set()

    def apply_theme(self, theme: str) -> None:
        pass


def _set_app_user_model_id() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:  # noqa: BLE001
        log.exception("failed to set AppUserModelID")


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="remi")
    p.add_argument("--browser", action="store_true", help="open the UI in the default browser (development)")
    p.add_argument("--background", action="store_true", help="start hidden in the tray (used by autostart)")
    p.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return p.parse_args(argv[1:])


def run(argv: list[str]) -> int:
    args = parse_args(argv)
    setup_logging()
    _set_app_user_model_id()

    single = SingleInstance()
    if single.notify_existing():
        log.info("another instance is running; asked it to show itself")
        return 0

    try:
        db = open_database(db_path())
    except SchemaTooNewError:
        lang = resolve_language("system")
        windows.message_box(APP_NAME, t("app.schemaTooNew", lang))
        return 1
    ctx = AppContext(db)
    sync = SyncManager(db, ctx.bus)
    window = BrowserWindow() if args.browser else WebviewWindow(ctx, sync, start_hidden=args.background)
    api = RemiApi(ctx, sync, window, autostart=windows.set_autostart)
    server = LocalServer(api, port=args.port)
    server.start()
    single.listen(window.show)

    def open_issue(server_id: int, issue_id: int) -> None:
        window.show()
        ctx.bus.publish("openIssue", serverId=server_id, issueId=issue_id)

    notifier = Notifier(ctx, api, open_issue)
    tray = None
    if not args.browser:
        tray = Tray(ctx, on_open=window.show, on_sync=sync.sync_now, on_quit=window.quit, notifier=notifier,
                    on_settings=lambda: (window.show(), ctx.bus.publish("openSettings")))
        window.minimize_to_tray = tray.start()
        # トレイがない環境で非表示のまま起動すると操作できなくなるので、その場合は表示する
        window.start_hidden = args.background and window.minimize_to_tray
        if ctx.settings.get("autostart"):
            windows.set_autostart(True)  # exeの場所が変わっても登録を最新にする
    sync.restart()
    try:
        window.run(server.start_url)
    except Exception:
        log.exception("could not start the window")
        windows.message_box(APP_NAME, t("app.webview2Missing", api.lang))
        return 1
    finally:
        if tray is not None and tray.icon is not None:
            tray.icon.stop()
        sync.stop_all(timeout=2.0)
        server.stop()
        single.close()
        db.close_all()
    return 0
