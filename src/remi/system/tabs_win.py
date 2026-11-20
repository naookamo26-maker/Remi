"""Redmineのタブを実際に表示する部分(Windows の WebView2。設計書 §5.4)。

本体のウィンドウ(pywebview のフォーム)に、タブごとの WebView2 の部品を足し、選んでいるタブだけを見せる:
- 「Remi」のタブ(本体の画面)を選んでいるとき: 本体の画面がフォーム全体に広がり、Redmineの部品はすべて隠す
- Redmineのタブを選んでいるとき: 本体の画面は上のタブ・操作行の高さだけにし、残りにそのタブの部品を広げる

WebView2 は、作ったスレッドからしか操作できないので、操作はフォームのスレッドに回す(Invoke)。
ここは Windows の実機でしか動かせない。失敗しても本体が壊れないよう、例外は呼んだ側(TabController)が受けて、別ウィンドウに戻す。
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import Any

from remi.system.tabs import Tab, TabController

log = logging.getLogger(__name__)

DEFAULT_CHROME_HEIGHT = 76.0  # タブの列+操作行の高さ(CSSのpx)。画面が実際の高さを知らせるまでの仮の値


def _net() -> tuple[Any, Any, Any, Any]:
    """pywebview が読み込んだ .NET の型(Windows でだけ使える)。"""
    import System.Windows.Forms as WinForms  # type: ignore[import-not-found]
    from Microsoft.Web.WebView2.WinForms import WebView2  # type: ignore[import-not-found]
    from System import Action, Uri  # type: ignore[import-not-found]

    return WinForms, WebView2, Action, Uri


def _apply_browser_features(core: Any) -> None:
    """パスワードの保存・自動入力、右クリックメニュー、ブラウザのショートカットを使えるようにする(別ウィンドウと同じ)。"""
    s = core.Settings
    s.IsPasswordAutosaveEnabled = True
    s.IsGeneralAutofillEnabled = True
    s.AreDefaultContextMenusEnabled = True
    s.AreBrowserAcceleratorKeysEnabled = True


class WinTabHost:
    """TabHost(system/tabs.py)の Windows 版。"""

    def __init__(self, form: Any, controller: TabController) -> None:
        self._WinForms, self._WebView2, self._Action, self._Uri = _net()
        self.form = form
        self.controller = controller
        self.app_view = form.browser.webview  # 本体の画面(pywebview の WebView2)
        self._views: dict[int, Any] = {}
        self._first_load_done: set[int] = set()
        self._chrome = DEFAULT_CHROME_HEIGHT

    # --- フォームのスレッドで動かす -----------------------------------------------------
    def _ui(self, fn: Callable[[], Any]) -> Any:
        result: list[Any] = []

        def run() -> None:
            result.append(fn())

        if self.form.InvokeRequired:
            self.form.Invoke(self._Action(run))
        else:
            run()
        return result[0] if result else None

    def _scale(self) -> float:
        return float(getattr(self.form, "DeviceDpi", 96)) / 96.0

    # --- TabHost -------------------------------------------------------------------------
    def open(self, tab: Tab) -> bool:
        def create() -> bool:
            wf = self._WinForms
            view = self._WebView2()
            # 本体の画面と同じ設定(データの保存先を含む)を使う。ログイン状態やパスワードが、本体・別ウィンドウと共通になる
            view.CreationProperties = self.app_view.CreationProperties
            view.Visible = False
            view.Dock = wf.DockStyle.Fill
            self.form.Controls.Add(view)
            self._views[tab.id] = view
            tid = tab.id
            view.CoreWebView2InitializationCompleted += lambda s, e: self._on_ready(tid, view, e)
            view.NavigationStarting += lambda s, e: self.controller.update(tid, loading=True)
            view.NavigationCompleted += lambda s, e: self._on_navigated(tid, view)
            view.SourceChanged += lambda s, e: self.controller.update(tid, url=str(view.Source))
            view.Source = self._Uri(tab.url)  # 部品の準備ができたら、このURLを開く
            self._layout()
            return True

        return bool(self._ui(create))

    def activate(self, tab_id: int | None) -> None:
        self._ui(self._layout)

    def close(self, tab_id: int) -> None:
        def remove() -> None:
            view = self._views.pop(tab_id, None)
            self._first_load_done.discard(tab_id)
            if view is not None:
                self.form.Controls.Remove(view)
                view.Dispose()
            self._layout()

        self._ui(remove)

    def action(self, tab_id: int, action: str) -> None:
        def run() -> None:
            view = self._views.get(tab_id)
            if view is None:
                return
            if action == "back" and view.CanGoBack:
                view.GoBack()
            elif action == "forward" and view.CanGoForward:
                view.GoForward()
            elif action == "reload":
                view.Reload()

        self._ui(run)

    def set_chrome_height(self, height: float) -> None:
        if height > 0 and abs(height - self._chrome) >= 0.5:
            self._chrome = height
            self._ui(self._layout)

    def dispose(self) -> None:
        def run() -> None:
            for view in list(self._views.values()):
                self.form.Controls.Remove(view)
                view.Dispose()
            self._views.clear()
            self._layout()

        self._ui(run)

    # --- 配置 -----------------------------------------------------------------------------
    def _layout(self) -> None:
        wf = self._WinForms
        active = self.controller.model.active
        controls = self.form.Controls
        self.form.SuspendLayout()
        try:
            for tid, view in self._views.items():
                view.Visible = tid == active
            if active is not None and active in self._views:
                view = self._views[active]
                # 本体の画面は上のタブ・操作行の高さだけ、残りをRedmineのページに使う。
                # Dock は、重なりの後ろの部品から順に決まるので、ページを手前、本体の画面を奥に置く
                self.app_view.Dock = wf.DockStyle.Top
                self.app_view.Height = max(1, int(self._chrome * self._scale()))
                view.Dock = wf.DockStyle.Fill
                controls.SetChildIndex(view, 0)
                controls.SetChildIndex(self.app_view, controls.Count - 1)
                view.Focus()
            else:
                self.app_view.Dock = wf.DockStyle.Fill
                controls.SetChildIndex(self.app_view, 0)
                self.app_view.Focus()
        finally:
            self.form.ResumeLayout(True)

    # --- 部品からの通知(フォームのスレッドで呼ばれる) ----------------------------------------
    def _on_ready(self, tab_id: int, view: Any, args: Any) -> None:
        if not args.IsSuccess:
            log.warning("WebView2 for a Redmine tab could not start")
            self.controller.failed(tab_id)
            return
        try:
            core = view.CoreWebView2
            _apply_browser_features(core)
            core.DocumentTitleChanged += lambda s, e: self._on_title(tab_id, core)
            core.HistoryChanged += lambda s, e: self._on_history(tab_id, view)
            core.NewWindowRequested += lambda s, e: self._on_new_window(e)
        except Exception:  # noqa: BLE001 - 設定できなくても、ページは見られる
            log.warning("could not set up a Redmine tab", exc_info=True)

    def _on_title(self, tab_id: int, core: Any) -> None:
        title = str(core.DocumentTitle or "")
        self.controller.update(tab_id, title=title)

    def _on_history(self, tab_id: int, view: Any) -> None:
        self.controller.update(tab_id, can_back=bool(view.CanGoBack), can_forward=bool(view.CanGoForward))

    def _on_navigated(self, tab_id: int, view: Any) -> None:
        self.controller.update(tab_id, loading=False, can_back=bool(view.CanGoBack),
                               can_forward=bool(view.CanGoForward))
        if tab_id not in self._first_load_done:
            # 設定(パスワードの保存など)は次の読み込みから効くので、最初の読み込みが終わったら一度だけ読み直す
            self._first_load_done.add(tab_id)
            try:
                view.Reload()
            except Exception:  # noqa: BLE001
                log.debug("could not reload a Redmine tab", exc_info=True)

    def _on_new_window(self, args: Any) -> None:
        """ページ内の「新しいウィンドウで開く」リンク: 新しいタブで開く。"""
        args.Handled = True
        uri = str(args.Uri)
        if uri.lower().startswith(("http://", "https://")):
            self.controller.open(uri)


CHROME_HEIGHT = 40.0  # 切り離したウィンドウの上の操作行の高さ(CSSのpx)


class WindowChrome:
    """Redmineを開いた別ウィンドウの上の、操作行(HTMLの画面。web/chrome.html)。

    ウィンドウのページの上に、もう1つの WebView2 を置き、操作行の画面を開く。ボタンの操作は、画面から API(windowAction)で
    受けて、action() でページに伝える。ページの状態(URL・戻る/進む・読み込み中)は、変わるたびに on_state で知らせる。
    ウィンドウの準備ができたあとに、一度だけ足す。足せなくても、ページはそのまま見られる。Windows 以外では何もしない。
    """

    def __init__(self, window: Any, window_id: int, chrome_url: str,
                 on_state: Callable[[int, dict[str, Any]], None], on_attach: Callable[[str], None] | None = None) -> None:
        self.window = window
        self.window_id = window_id
        self.chrome_url = chrome_url
        self.on_state = on_state
        self.on_attach = on_attach
        self._built = False
        self._loading = False
        self._form: Any = None
        self._page: Any = None
        if sys.platform == "win32":
            window.events.loaded += self._on_loaded

    def _on_loaded(self) -> None:
        if self._built:
            return
        self._built = True
        try:
            wf, webview2, action, uri = _net()
            form = self.window.native

            def build() -> None:
                page = form.browser.webview
                scale = float(getattr(form, "DeviceDpi", 96)) / 96.0
                chrome = webview2()
                # ページと同じ設定(データの保存先)を使う。本体の画面が受け取った認証のCookieを、この画面も使える
                chrome.CreationProperties = page.CreationProperties
                chrome.Dock = wf.DockStyle.Top
                chrome.Height = max(1, int(CHROME_HEIGHT * scale))
                form.Controls.Add(chrome)
                # Dock は後ろの部品から順に決まる。操作行を奥にして、ページを残りいっぱいに
                form.Controls.SetChildIndex(chrome, form.Controls.Count - 1)
                chrome.Source = uri(self.chrome_url)
                self._form, self._page = form, page
                page.SourceChanged += lambda s, e: self._push()
                page.NavigationStarting += lambda s, e: self._push(loading=True)
                page.NavigationCompleted += lambda s, e: self._push(loading=False)
                self._push()

            form.Invoke(action(build))
        except Exception:  # noqa: BLE001 - 足せなくても、ページは見られる
            log.warning("could not add the toolbar to the Redmine window", exc_info=True)

    def _push(self, loading: bool | None = None) -> None:
        if loading is not None:
            self._loading = loading
        page = self._page
        if page is None:
            return
        self.on_state(self.window_id, {
            "url": str(page.Source) if page.Source is not None else "", "canBack": bool(page.CanGoBack),
            "canForward": bool(page.CanGoForward), "loading": self._loading, "attachable": self.on_attach is not None})

    def action(self, name: str) -> None:
        """操作行のボタン: back / forward / reload / attach。フォームのスレッドで動かす。"""
        if self._form is None or self._page is None:
            return
        _wf, _w, action, _u = _net()

        def run() -> None:
            page = self._page
            if name == "back" and page.CanGoBack:
                page.GoBack()
            elif name == "forward" and page.CanGoForward:
                page.GoForward()
            elif name == "reload":
                page.Reload()
            elif name == "attach" and self.on_attach is not None:
                self.on_attach(str(page.Source))

        self._form.Invoke(action(run))
