"""Windows の WebView2 の設定(設計書 §5.4)。pywebview が出していない設定を、ウィンドウの中の部品から直接変える。"""

from __future__ import annotations

import logging
import sys
from typing import Any

log = logging.getLogger(__name__)


def enable_browser_features(window: Any) -> None:
    """Redmineのページを開くウィンドウで、標準的なブラウザと同じ機能を使えるようにする。

    - パスワードの保存(「パスワードを保存しますか?」)と、フォームの自動入力
    - 右クリックメニュー(戻る・リンクのコピー・印刷など)と、ブラウザのショートカット(Ctrl+F・Alt+← など)

    保存したパスワードは、WebView2 のデータ(Remiのデータフォルダ。Windowsの DPAPI で暗号化される)に残る。
    設定は次の読み込みから効くので、最初の読み込みが終わったら一度だけ読み直す(最初のログイン画面でも保存できるように)。
    Windows 以外(開発モード)では何もしない。
    """
    if sys.platform != "win32":
        return
    state = {"done": False}

    def on_loaded() -> None:
        if state["done"]:
            return
        state["done"] = True
        try:
            from System import Action  # pythonnet(pywebview の Windows 版が使う)

            form = window.native

            def apply() -> None:
                core = form.browser.webview.CoreWebView2
                s = core.Settings
                s.IsPasswordAutosaveEnabled = True
                s.IsGeneralAutofillEnabled = True
                s.AreDefaultContextMenusEnabled = True
                s.AreBrowserAcceleratorKeysEnabled = True
                core.Reload()

            # WebView2 は、作った画面のスレッドからしか操作できない
            form.Invoke(Action(apply))
        except Exception:  # noqa: BLE001 - 設定できなくても、ページは見られる
            log.warning("could not enable browser features in the Redmine window", exc_info=True)

    window.events.loaded += on_loaded
