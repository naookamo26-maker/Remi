"""Redmineのウィンドウで、標準的なブラウザと同じ機能を使えるようにする設定(src/remi/system/webview2.py)。"""

import sys
import types

from remi.system import webview2


class _Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self

    def fire(self):
        for fn in self.handlers:
            fn()


def _fake_window():
    settings = types.SimpleNamespace()
    core = types.SimpleNamespace(Settings=settings, reloads=0)
    core.Reload = lambda: setattr(core, "reloads", core.reloads + 1)
    form = types.SimpleNamespace(browser=types.SimpleNamespace(webview=types.SimpleNamespace(CoreWebView2=core)),
                                 invoked=0)

    def invoke(action):
        form.invoked += 1
        action()

    form.Invoke = invoke
    window = types.SimpleNamespace(native=form, events=types.SimpleNamespace(loaded=_Event()))
    return window, core, settings, form


def test_enables_password_autosave_and_context_menu_once(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "System", types.SimpleNamespace(Action=lambda fn: fn))
    window, core, settings, form = _fake_window()
    webview2.enable_browser_features(window)
    window.events.loaded.fire()
    assert settings.IsPasswordAutosaveEnabled and settings.IsGeneralAutofillEnabled
    assert settings.AreDefaultContextMenusEnabled and settings.AreBrowserAcceleratorKeysEnabled
    assert core.reloads == 1 and form.invoked == 1  # 画面のスレッドで設定し、一度だけ読み直す
    window.events.loaded.fire()
    assert core.reloads == 1 and form.invoked == 1


def test_does_nothing_outside_windows_and_survives_errors(monkeypatch):
    window, core, settings, form = _fake_window()
    monkeypatch.setattr(sys, "platform", "linux")
    webview2.enable_browser_features(window)
    assert window.events.loaded.handlers == []

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "System", types.SimpleNamespace(Action=lambda fn: fn))
    window.native = None  # 部品に手が届かなくても、ページの表示は止めない
    webview2.enable_browser_features(window)
    window.events.loaded.fire()
