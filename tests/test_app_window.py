"""本体のウィンドウとRedmineのウィンドウの連動(設計書 §5.4)。実際のウィンドウの代わりに、記録するだけの偽物を使う。"""

from remi.app import WebviewWindow


class FakeWindow:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def show(self) -> None:
        self.calls.append("show")

    def hide(self) -> None:
        self.calls.append("hide")

    def restore(self) -> None:
        self.calls.append("restore")


class FakeSync:
    def __init__(self) -> None:
        self.visible: list[bool] = []

    def set_visible(self, visible: bool) -> None:
        self.visible.append(visible)


def make():
    w = WebviewWindow(ctx=None, sync=FakeSync())  # type: ignore[arg-type]
    w.window = FakeWindow()
    w.redmine_window = FakeWindow()
    return w


def test_redmine_window_is_hidden_and_restored_with_the_main_window():
    w = make()
    w.hide()
    # 本体をトレイに格納すると、Redmineのウィンドウも隠れる(本体だけが消えて、Redmineのウィンドウが残らない)
    assert w.window.calls == ["hide"] and w.redmine_window.calls == ["hide"]
    w.show()
    assert w.window.calls == ["hide", "show", "restore"]
    assert w.redmine_window.calls == ["hide", "show", "restore"]  # 本体を開くと、戻る
    # 戻したあとにもう一度本体を開いても、Redmineのウィンドウは触らない
    w.show()
    assert w.redmine_window.calls == ["hide", "show", "restore"]


def test_redmine_window_that_was_not_open_is_left_alone():
    w = make()
    w.redmine_window = None
    w.hide()
    w.show()  # Redmineのウィンドウがなくても、エラーにならない
    assert w.window.calls == ["hide", "show", "restore"]


def test_closing_the_redmine_window_while_hidden_does_not_reopen_it():
    w = make()
    w.hide()
    w.redmine_window = None  # 隠れている間に、閉じられた
    w._redmine_hidden = False
    w.show()
    assert w.redmine_window is None


class _Hook:
    """pywebview のイベント(`events.shown += 関数`)の偽物。足した関数を記録する。"""

    def __init__(self) -> None:
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self


class FakeEvents:
    def __init__(self) -> None:
        self.shown = _Hook()


class FakeNative:
    def __init__(self, hwnd: int) -> None:
        self.Handle = type("H", (), {"ToInt64": lambda self_: hwnd})()


def test_title_bar_follows_the_app_theme_for_every_window(monkeypatch):
    """タイトルバー(最小化などのボタンがある部分)の色を、本体とRedmineのすべてのウィンドウで、アプリのテーマに合わせる。"""
    from remi import app as app_module

    calls = []
    monkeypatch.setattr(app_module.sys, "platform", "win32")
    monkeypatch.setattr(app_module.windows, "set_dark_title_bar", lambda hwnd, dark: calls.append((hwnd, dark)))
    w = make()
    w.window.native = FakeNative(1)
    w.redmine_window.native = FakeNative(2)
    detached = FakeWindow()
    detached.native = FakeNative(3)
    w._detached = [detached]
    w._dark = lambda: True
    w.apply_theme("dark")
    assert sorted(calls) == [(1, True), (2, True), (3, True)]
    # テーマを切り替えると、すべてのウィンドウが追従する
    calls.clear()
    w._dark = lambda: False
    w.apply_theme("light")
    assert sorted(calls) == [(1, False), (2, False), (3, False)]
    # 新しく開いたウィンドウは、表示されたときに、アプリのテーマの色になる
    calls.clear()
    new = FakeWindow()
    new.native = FakeNative(4)
    new.events = FakeEvents()
    w._dark = lambda: True
    w._style_redmine_window(new)
    for handler in new.events.shown.handlers:
        handler()
    assert calls == [(4, True)]


def test_title_bar_is_applied_again_and_again(monkeypatch):
    """タイトルバーの色が、しばらくして既定の色に戻っても、付け直す(設定しているなら、同じ値を渡すだけ)。"""
    from remi import app as app_module

    calls = []
    monkeypatch.setattr(app_module.sys, "platform", "win32")
    monkeypatch.setattr(app_module.windows, "set_dark_title_bar", lambda hwnd, dark: calls.append((hwnd, dark)))
    w = make()
    w.window.native = FakeNative(1)
    w._dark = lambda: True
    ticks = []

    def fake_sleep(seconds):
        ticks.append(seconds)
        if len(ticks) == 3:  # 3回目の待ちのあとで終わる
            w._quitting = True

    monkeypatch.setattr(app_module.time, "sleep", fake_sleep)
    w._keep_title_bar()
    assert ticks == [w.TITLE_BAR_INTERVAL] * 3
    assert (1, True) in calls and len([c for c in calls if c[0] == 1]) >= 3
