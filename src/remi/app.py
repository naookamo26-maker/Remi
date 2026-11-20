"""アプリの組み立てと起動。"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
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
from remi.system.notifier import PROTOCOL, Notifier, parse_open_url
from remi.system.single_instance import SingleInstance
from remi.system.tabs import TabController
from remi.system.tabs_win import WinTabHost, WindowChrome
from remi.system.tray import Tray
from remi.system.webview2 import enable_browser_features

log = logging.getLogger(__name__)


class WebviewWindow:
    """pywebview(WebView2)のウィンドウ(設計書 §2.6, §8)。"""

    def __init__(self, ctx: AppContext, sync: SyncManager, start_hidden: bool = False) -> None:
        self.ctx = ctx
        self.sync = sync
        self.start_hidden = start_hidden
        self.window = None
        self.redmine_window = None  # Redmineのページを開くウィンドウ(設計書 §5.4)
        self._redmine_hidden = False  # 本体と一緒に隠している(本体を開いたら戻す)
        self._detached: list = []  # タブから切り離した、Redmineのウィンドウ
        self._tabs: TabController | None = None
        self._tabs_failed = False
        self.tabs_error: str | None = None  # タブを使えなかった理由(設定の画面に出す)
        self._origin: str | None = None  # ローカルサーバーのオリジン(操作行の画面のURLに使う)
        self._chromes: dict[int, WindowChrome] = {}  # Redmineのウィンドウごとの操作行
        self._window_states: dict[int, dict] = {}
        self._next_window_id = 1
        self.minimize_to_tray = False  # トレイが使えるときに True にする
        self._quitting = False

    def run(self, url: str) -> None:
        import webview
        from urllib.parse import urlparse

        parsed = urlparse(url)
        self._origin = f"{parsed.scheme}://{parsed.netloc}"

        self.window = webview.create_window(
            f"{APP_NAME} v{__version__}", url, width=1280, height=800, min_size=(900, 560), hidden=self.start_hidden,
            background_color="#1E1F22" if self._dark() else "#FFFFFF", text_select=True,
        )
        if self.start_hidden:
            self.sync.set_visible(False)
        self.window.events.closing += self._on_closing
        self.window.events.shown += lambda: self.apply_theme("")
        gui = "edgechromium" if sys.platform == "win32" else None
        if sys.platform == "win32":
            threading.Thread(target=self._keep_title_bar, name="title-bar", daemon=True).start()
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
            self.sync.set_visible(True)
            self._show_redmine_window()
            self.apply_theme("")  # 隠している間に戻った色を付け直す

    def hide(self) -> None:
        if self.window is not None:
            self.window.hide()
            self._hide_redmine_window()  # 本体をトレイに格納したら、Redmineのウィンドウだけが残らないようにする
            self.sync.set_visible(False)  # トレイに格納している間は、開いているチケットを確かめない

    def _redmine_windows(self) -> list:
        """Redmineを開いているウィンドウ(別ウィンドウと、タブから切り離したもの)。"""
        return [w for w in (self.redmine_window, *self._detached) if w is not None]

    def _hide_redmine_window(self) -> None:
        """本体と一緒に、Redmineのウィンドウも隠す(本体を開いたとき戻す)。"""
        windows_ = self._redmine_windows()
        if not windows_ or self._redmine_hidden:
            return
        for w in windows_:
            try:
                w.hide()
                self._redmine_hidden = True
            except Exception:  # noqa: BLE001 - 隠せなくても、本体の格納は続ける
                log.debug("could not hide the Redmine window", exc_info=True)

    def _show_redmine_window(self) -> None:
        if not self._redmine_hidden:
            return
        self._redmine_hidden = False
        for w in self._redmine_windows():
            try:
                w.show()
                w.restore()
            except Exception:  # noqa: BLE001
                log.debug("could not show the Redmine window", exc_info=True)

    def quit(self) -> None:
        self._quitting = True
        if self._tabs is not None:
            self._tabs.dispose()
        for w in self._redmine_windows():
            try:
                w.destroy()
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
                self._redmine_hidden = False
                return True
            w = webview.create_window(f"{APP_NAME} — Redmine", url, width=1100, height=800, min_size=(640, 480),
                                      text_select=True)

            def closed() -> None:
                self.redmine_window = None
                self._redmine_hidden = False

            w.events.closed += closed
            # パスワードの保存・自動入力、右クリックメニューなど、標準的なブラウザと同じ機能を使えるようにする
            enable_browser_features(w)
            self._style_redmine_window(w)
            self._add_toolbar(w)
            self.redmine_window = w
            return True
        except Exception:  # noqa: BLE001 - 開けなければ既定のブラウザで開く
            log.exception("could not open the Redmine window")
            self.redmine_window = None
            return False

    def open_detached(self, url: str) -> bool:
        """タブから切り離したRedmineのページを、新しいウィンドウで開く(ウィンドウは、いくつでも開ける)。"""
        import webview

        try:
            w = webview.create_window(f"{APP_NAME} — Redmine", url, width=1100, height=800, min_size=(640, 480),
                                      text_select=True)

            def closed() -> None:
                if w in self._detached:
                    self._detached.remove(w)

            w.events.closed += closed
            enable_browser_features(w)
            self._style_redmine_window(w)
            self._add_toolbar(w)
            self._detached.append(w)
            return True
        except Exception:  # noqa: BLE001
            log.exception("could not open a detached Redmine window")
            return False

    # --- タブ(設計書 §5.4) ---------------------------------------------------------------
    @property
    def tabs(self) -> TabController | None:
        """Redmineをタブで開く窓口。Windows 以外、または部品を作れなかったときは None(別ウィンドウで開く)。"""
        if self._tabs is None and not self._tabs_failed and sys.platform == "win32" and self.window is not None:
            try:
                controller = TabController(on_changed=self._publish_tabs, on_failed=webbrowser.open)
                controller.host = WinTabHost(self.window.native, controller)
                self._tabs = controller
            except Exception as e:  # noqa: BLE001 - タブにできなければ、別ウィンドウで開く
                log.warning("could not set up the Redmine tabs", exc_info=True)
                self._tabs_failed = True
                self.tabs_error = f"{type(e).__name__}: {e}"
        return self._tabs

    def _publish_tabs(self) -> None:
        if self._tabs is not None:
            self.ctx.bus.publish("tabsChanged", **self._tabs.snapshot())

    def _add_toolbar(self, w) -> None:
        """Redmineのウィンドウの上に、操作行(戻る・進む・再読み込み・URL・「タブに戻す」。HTMLの画面)を足す。"""
        if self._origin is None:
            return
        window_id = self._next_window_id
        self._next_window_id += 1
        attach = (lambda url: self._attach_window(w, url)) if self.tabs is not None else None
        self._chromes[window_id] = WindowChrome(w, window_id, f"{self._origin}/chrome.html?w={window_id}",
                                                self._on_window_state, attach)

        def closed() -> None:
            self._chromes.pop(window_id, None)
            self._window_states.pop(window_id, None)

        w.events.closed += closed

    def _on_window_state(self, window_id: int, state: dict) -> None:
        self._window_states[window_id] = state
        self.ctx.bus.publish("windowState", windowId=window_id, **state)

    def window_action(self, window_id: int, action: str) -> None:
        chrome = self._chromes.get(window_id)
        if chrome is not None:
            chrome.action(action)

    def window_state(self, window_id: int) -> dict | None:
        return self._window_states.get(window_id)

    def _attach_window(self, w, url: str) -> None:
        """切り離したウィンドウを閉じて、同じページをタブで開く。"""
        tabs = self.tabs
        if tabs is None or not tabs.open(url):
            return
        try:
            w.destroy()
        except Exception:  # noqa: BLE001
            log.debug("could not close the detached window", exc_info=True)
        self.show()

    def apply_theme(self, theme: str) -> None:
        """タイトルバー(最小化などのボタンがある部分)の色を、アプリのテーマ(システムに従う設定を含む)に合わせる。
        本体と、Redmineを開いた別のウィンドウのすべてに適用する。"""
        if sys.platform != "win32":
            return
        for w in (self.window, *self._redmine_windows()):
            self._set_title_bar(w)

    TITLE_BAR_INTERVAL = 2.0  # タイトルバーの色を確かめ直す間隔(秒)

    def _keep_title_bar(self) -> None:
        """タイトルバーの色を、定期的に付け直す。

        ウィンドウの色の設定は、しばらく使わなかったり、隠して出し直したり、ウィンドウが作り直されたりすると、
        Windows 側で既定の色に戻ることがある。戻っても気づけないので、設定していれば何も起きない(同じ値を渡すだけ)
        付け直しを続ける。システムの色に従う設定のとき、システムの色の切り替えにも追従する。
        """
        while not self._quitting:
            time.sleep(self.TITLE_BAR_INTERVAL)
            try:
                self.apply_theme("")
            except Exception:  # noqa: BLE001 - 色が付け直せなくても、アプリは動かし続ける
                log.debug("could not refresh the title bar", exc_info=True)

    def _set_title_bar(self, w) -> None:
        if w is None:
            return
        try:
            hwnd = int(w.native.Handle.ToInt64())
        except Exception:  # noqa: BLE001
            return
        windows.set_dark_title_bar(hwnd, self._dark())

    def _style_redmine_window(self, w) -> None:
        """Redmineを開いたウィンドウのタイトルバーを、アプリのテーマの色にする(開いたとき)。"""
        if sys.platform == "win32":
            w.events.shown += lambda: self._set_title_bar(w)


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
    # 通知を押したとき(remi: プロトコル)に、Windowsが渡すURL
    p.add_argument("--open", default=None, metavar="URL", help=argparse.SUPPRESS)
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return p.parse_args(argv[1:])


def run(argv: list[str]) -> int:
    args = parse_args(argv)
    setup_logging()
    _set_app_user_model_id()

    # DBを開くなどの準備より先に待ち受ける(同時に起動したとき、両方が起動したままにならないよう、間を短くする)。
    # ウィンドウを作るまでに届いた前面表示の依頼は、覚えておいて起動時に表示する
    show_requested = threading.Event()
    show_target: list = []
    # 通知を押して起動された(または起動中のRemiへ届いた)ときに開くチケット
    open_target = parse_open_url(args.open) if args.open else None
    pending_open: list[tuple[int, int]] = [open_target] if open_target else []

    def on_show(message: object = "show") -> None:
        if isinstance(message, tuple) and message[0] == "open":
            if len(show_target) > 1:
                show_target[1](message[1], message[2])
                return
            pending_open.append((message[1], message[2]))
        if show_target:
            show_target[0]()
        else:
            show_requested.set()

    single = SingleInstance()
    if not single.acquire(on_show, ("open", *open_target) if open_target else "show"):
        log.info("another instance is running; asked it to show itself")
        return 0

    try:
        db = open_database(db_path())
    except SchemaTooNewError:
        single.close()
        lang = resolve_language("system")
        windows.message_box(APP_NAME, t("app.schemaTooNew", lang))
        return 1
    ctx = AppContext(db)
    sync = SyncManager(db, ctx.bus)
    window = BrowserWindow() if args.browser else WebviewWindow(ctx, sync, start_hidden=args.background)
    api = RemiApi(ctx, sync, window, autostart=windows.set_autostart)
    server = LocalServer(api, port=args.port)
    server.start()
    if pending_open:
        show_requested.set()
    if show_requested.is_set() and isinstance(window, WebviewWindow):
        window.start_hidden = False  # 起動中に、もう一度起動された(前面表示の依頼が来た)

    def open_issue(server_id: int, issue_id: int) -> None:
        window.show()
        ctx.bus.publish("openIssue", serverId=server_id, issueId=issue_id)

    # 画面が立ち上がる前に届いた「チケットを開く」は、画面が起動時に受け取る(takePendingOpen)
    api.pending_open = pending_open
    show_target.extend([window.show, open_issue])
    # 通知センターでアプリ名・アイコンを出し、履歴から押しても届くよう、起動のたびに登録し直す
    windows.register_notifications(APP_USER_MODEL_ID, APP_NAME, WebviewWindow._icon_path(), PROTOCOL)
    notifier = Notifier(ctx, api, open_issue)
    tray = None
    if not args.browser:
        tray = Tray(ctx, on_open=window.show, on_sync=sync.sync_now, on_quit=window.quit, notifier=notifier,
                    on_settings=lambda: (window.show(), ctx.bus.publish("openSettings")))
        window.minimize_to_tray = tray.start()
        # トレイがない環境で非表示のまま起動すると操作できなくなるので、その場合は表示する
        window.start_hidden = args.background and window.minimize_to_tray and not show_requested.is_set()
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
