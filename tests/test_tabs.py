"""Redmineのタブの状態管理(設計書 §5.4)。ページを表示する部品(WebView2)は Windows 専用なので、ここでは偽物を使う。"""

import pytest

from remi.system.tabs import TabController, TabModel


def test_model_opens_activates_and_closes_like_a_browser():
    m = TabModel()
    assert m.active is None and m.snapshot() == {"tabs": [], "active": None}
    a = m.open("https://rm/a")
    b = m.open("https://rm/b")
    c = m.open("https://rm/c")
    assert (a.id, b.id, c.id) == (1, 2, 3) and m.active == 3  # 新しいタブを選ぶ(いちばん右)
    assert m.activate(1) is True and m.activate(1) is False  # 変わらないときは False
    assert m.activate(99) is False and m.active == 1  # 知らないタブは選べない
    m.activate(2)
    assert m.close(2).url == "https://rm/b" and m.active == 3  # 選んでいたタブを閉じると、右隣
    assert m.close(3) and m.active == 1  # 右がなければ左隣
    assert m.close(1) and m.active is None  # すべて閉じると、「Remi」のタブ
    assert m.close(1) is None
    assert m.activate(None) is False


def test_closing_an_unselected_tab_keeps_the_selection():
    m = TabModel()
    m.open("https://rm/a")
    m.open("https://rm/b")
    m.activate(1)
    m.close(2)
    assert m.active == 1


def test_model_update_reports_changes_only():
    m = TabModel()
    t = m.open("https://rm/a")
    assert t.loading is True and t.title == ""
    assert m.update(t.id, title="チケット #1", loading=False) is True
    assert m.update(t.id, title="チケット #1") is False
    assert m.update(99, title="x") is False
    assert m.snapshot()["tabs"][0] == {"id": 1, "url": "https://rm/a", "title": "チケット #1", "canBack": False,
                                       "canForward": False, "loading": False}


class FakeHost:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def open(self, tab):
        self.calls.append(("open", tab.id, tab.url))
        return self.ok

    def activate(self, tab_id):
        self.calls.append(("activate", tab_id))

    def close(self, tab_id):
        self.calls.append(("close", tab_id))

    def action(self, tab_id, action):
        self.calls.append(("action", tab_id, action))

    def set_chrome_height(self, height):
        self.calls.append(("height", height))

    def dispose(self):
        self.calls.append(("dispose",))


def test_controller_drives_the_host_and_announces_changes():
    changes = []
    host = FakeHost()
    c = TabController(on_changed=lambda: changes.append(c.snapshot()["active"]), host=host)
    assert c.open("https://rm/a") is True and c.open("https://rm/b") is True
    c.activate(1)
    c.close(1)
    assert host.calls == [("open", 1, "https://rm/a"), ("open", 2, "https://rm/b"), ("activate", 1),
                          ("close", 1), ("activate", 2)]
    assert changes == [1, 2, 1, 2]
    c.update(2, title="B", can_back=True)
    assert changes[-1] == 2 and c.snapshot()["tabs"][0]["canBack"] is True
    c.update(2, title="B")  # 変わらない更新は、画面に知らせない
    assert len(changes) == 5


def test_controller_actions_are_checked_and_forwarded():
    host = FakeHost()
    c = TabController(host=host)
    c.open("https://rm/a")
    host.calls.clear()
    c.action(1, "back")
    c.action(1, "forward")
    c.action(1, "reload")
    c.action(1, "rm -rf")  # 知らない操作は無視
    c.action(99, "back")  # 知らないタブも無視
    c.set_chrome_height(80)
    assert host.calls == [("action", 1, "back"), ("action", 1, "forward"), ("action", 1, "reload"), ("height", 80)]


def test_controller_falls_back_when_the_host_cannot_open():
    changes = []
    c = TabController(on_changed=lambda: changes.append(1), host=FakeHost(ok=False))
    assert c.open("https://rm/a") is False  # 呼んだ側が、別ウィンドウなどで開く
    assert c.snapshot() == {"tabs": [], "active": None} and changes == []


def test_controller_records_why_a_tab_could_not_be_opened():
    class Broken(FakeHost):
        def open(self, tab):
            raise RuntimeError("no WebView2")

    c = TabController(host=Broken())
    assert c.last_error is None
    c.open("https://rm/a")
    assert c.last_error == "RuntimeError: no WebView2"
    c2 = TabController(host=FakeHost(ok=False))
    c2.open("https://rm/a")
    assert c2.last_error == "the tab host could not open the page"
    c3 = TabController(host=FakeHost())
    c3.open("https://rm/a")
    c3.failed(1)
    assert c3.last_error == "WebView2 for the tab could not start"


def test_controller_survives_a_host_that_raises():
    class Broken(FakeHost):
        def open(self, tab):
            raise RuntimeError("boom")

        def close(self, tab_id):
            raise RuntimeError("boom")

    c = TabController(host=Broken())
    assert c.open("https://rm/a") is False
    c2 = TabController(host=FakeHost())
    c2.open("https://rm/a")
    c2.host = Broken()
    c2.close(1)  # 部品の失敗でも、タブの一覧からは消える
    assert c2.snapshot()["tabs"] == []


def test_failed_tab_is_closed_and_reported():
    failed = []
    c = TabController(on_failed=failed.append, host=FakeHost())
    c.open("https://rm/a")
    c.failed(1)  # 部品の準備が、あとで失敗した
    assert failed == ["https://rm/a"] and c.snapshot()["tabs"] == []
    c.failed(1)  # 二重に呼ばれても、一度だけ
    assert failed == ["https://rm/a"]


def test_detach_returns_the_url_and_closes_the_tab():
    c = TabController(host=FakeHost())
    c.open("https://rm/a")
    assert c.detach(1) == "https://rm/a" and c.snapshot()["tabs"] == []
    assert c.detach(1) is None


@pytest.mark.parametrize("without_callback", [True, False])
def test_controller_works_without_a_host_or_callback(without_callback):
    c = TabController(on_changed=None if without_callback else (lambda: 1 / 0))  # 通知が失敗しても、続ける
    assert c.open("https://rm/a") is True
    c.activate(None)
    assert c.snapshot()["active"] is None
