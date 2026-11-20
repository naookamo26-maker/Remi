"""イベントバスの同期の活動状態(画面の「読み込み中」の表示に使う)。"""

from remi.events import EventBus


def test_activity_is_recorded_and_announced_only_when_it_changes():
    bus = EventBus()
    sub = bus.subscribe()
    assert bus.activity(1) == set()
    bus.set_activity(1, "sync", True)
    bus.set_activity(1, "sync", True)  # 変わらないときは、画面に知らせない
    assert bus.activity(1) == {"sync"}
    bus.set_activity(1, "versions", True)
    assert bus.activity(1) == {"sync", "versions"} and bus.activity(2) == set()
    bus.set_activity(1, "sync", False)
    bus.set_activity(1, "sync", False)
    assert bus.activity(1) == {"versions"}
    events = []
    while (e := sub.get(timeout=0.05)) is not None:
        events.append(e)
    assert [e["type"] for e in events] == ["syncState"] * 3  # sync オン, versions オン, sync オフ
