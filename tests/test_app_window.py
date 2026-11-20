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
