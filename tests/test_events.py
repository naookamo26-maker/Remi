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


def test_overflow_drops_old_events_and_keeps_only_resync():
    bus = EventBus()
    sub = bus.subscribe()
    for i in range(sub.queue.maxsize):
        bus.publish("issue", issueId=i)
    bus.publish("issue", issueId=-1)  # あふれる
    assert sub.get(timeout=0.05) == {"type": "resync"}
    assert sub.get(timeout=0.05) is None  # 古いイベントは残らない
    bus.publish("syncState", serverId=1)  # その後は普通に届く
    assert sub.get(timeout=0.05)["type"] == "syncState"


def test_concurrent_publishers_overflow_leaves_exactly_one_resync_first():
    """複数のスレッドが同時にあふれさせても、resync は1つだけで、先頭に残る(古いイベントや二重の resync が混ざらない)。"""
    import queue
    import threading

    for _ in range(100):
        bus = EventBus()
        sub = bus.subscribe()
        sub.queue = queue.Queue(5)
        for i in range(5):
            bus.publish("old", n=i)  # いっぱい
        start = threading.Barrier(8)

        def publisher(k):
            start.wait()
            for i in range(20):
                bus.publish("new", k=k, i=i)

        threads = [threading.Thread(target=publisher, args=(k,)) for k in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        items = []
        while (e := sub.get(timeout=0)) is not None:
            items.append(e)
        assert [e["type"] for e in items].count("resync") == 1 and items[0]["type"] == "resync"
        assert all(e["type"] in ("resync", "new") for e in items)  # あふれる前の "old" は残らない
