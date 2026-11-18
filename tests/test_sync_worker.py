import threading
import time

from fake_redmine import ME

from remi.events import EventBus
from remi.store.issues import IssueStore
from remi.store.servers import Server, ServerStore
from remi.sync.worker import SyncManager, prune_attachment_cache


def wait_until(cond, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class Recorder:
    def __init__(self, bus):
        self.events = []
        self.lock = threading.Lock()
        bus.on(self._on)

    def _on(self, e):
        with self.lock:
            self.events.append(e)

    def of(self, type_):
        with self.lock:
            return [e for e in self.events if e["type"] == type_]


def test_manager_syncs_and_posts(db, fake):
    fake.add_project(1, "A")
    fake.add_issue(1, 1, "担当")
    fake.add_journal(1, 7, "既存のコメント")
    sid = ServerStore(db).save(Server(name="S", url=fake.base_url), api_key="test-key").server_id
    bus = EventBus()
    rec = Recorder(bus)
    mgr = SyncManager(db, bus)
    mgr.restart()
    try:
        store = IssueStore(db)
        assert wait_until(lambda: store.get(sid, 1) is not None and store.journals_synced(sid, 1))
        assert store.unread_count(sid, 1) == 0
        s = ServerStore(db).get(sid)
        assert s.my_user_id == ME and s.last_error is None and s.last_synced_at
        assert rec.of("syncState") and rec.of("dataChanged")

        store.save_draft(sid, 1, "返信します", False)
        assert mgr.post_note(sid, 1, "返信します", False)
        assert wait_until(lambda: rec.of("noteFinished"))
        assert rec.of("noteFinished")[0]["error"] is None
        assert wait_until(lambda: any(j.notes == "返信します" for j in store.journals(sid, 1)))
        assert store.get_draft(sid, 1) == ("", False, None)  # 送信できたら下書きは消える
    finally:
        mgr.stop_all()


def test_failed_post_keeps_draft(db, fake):
    fake.add_project(1, "A")
    fake.add_issue(1, 1, "担当")
    sid = ServerStore(db).save(Server(name="S", url=fake.base_url), api_key="test-key").server_id
    bus = EventBus()
    rec = Recorder(bus)
    mgr = SyncManager(db, bus)
    mgr.restart()
    try:
        store = IssueStore(db)
        assert wait_until(lambda: store.get(sid, 1) is not None)
        mgr.post_note(sid, 1, "   ", False)  # 空のコメントはサーバーが 422 を返す
        assert wait_until(lambda: rec.of("noteFinished"))
        assert rec.of("noteFinished")[0]["error"] == "unprocessable"
        body, private, failed = store.get_draft(sid, 1)
        assert failed is not None
    finally:
        mgr.stop_all()


def test_worker_records_error_and_backs_off(db, fake):
    sid = ServerStore(db).save(Server(name="S", url=fake.base_url), api_key="wrong").server_id
    mgr = SyncManager(db, EventBus())
    mgr.restart()
    try:
        assert wait_until(lambda: ServerStore(db).get(sid).last_error == "unauthorized")
        n = len(fake.requests)
        time.sleep(1.5)
        assert len(fake.requests) == n  # バックオフ中は問い合わせない
    finally:
        mgr.stop_all()


def test_attachment_download_and_cache_limit(db, fake):
    fake.add_project(1, "A")
    fake.add_issue(1, 1, "担当")
    fake.add_attachment(1, 9, "shot.png", b"\x89PNG" + b"0" * 2000)
    sid = ServerStore(db).save(Server(name="S", url=fake.base_url), api_key="test-key").server_id
    bus = EventBus()
    rec = Recorder(bus)
    mgr = SyncManager(db, bus)
    mgr.restart()
    try:
        store = IssueStore(db)
        assert wait_until(lambda: store.get_attachment(sid, 9) is not None)
        mgr.request_attachment(sid, 9)
        assert wait_until(lambda: rec.of("attachmentReady"))
        assert rec.of("attachmentReady")[0]["ok"] is True
        assert store.get_attachment(sid, 9).local_path.endswith(".png")
    finally:
        mgr.stop_all()
    prune_attachment_cache(db, limit_mb=0)
    assert store.get_attachment(sid, 9).local_path is None


def test_opening_issue_refetches_comments_even_if_unchanged(db, fake):
    """開いた直後は、更新日時が同じでも(同じ秒の更新を見逃していても)コメントごと取り直す。再起動の後でも。"""
    fake.add_project(1, "A")
    fake.add_issue(1, 1, "担当")
    sid = ServerStore(db).save(Server(name="S", url=fake.base_url), api_key="test-key").server_id
    bus = EventBus()
    mgr = SyncManager(db, bus)
    mgr.restart()
    store = IssueStore(db)
    try:
        assert wait_until(lambda: store.get(sid, 1) is not None and store.journals_synced(sid, 1))
    finally:
        mgr.stop_all()
    # 同期した後、同じ秒に入ったコメント(updated_on が変わらない)を、再起動の後に開く
    stamp = store.updated_on(sid, 1)
    fake.add_journal(1, 7, "同じ秒のコメント", created_on=stamp)
    assert fake.issues[1]["updated_on"] == stamp
    mgr = SyncManager(db, bus)
    mgr.restart()
    try:
        mgr.set_open_issue(sid, 1)
        assert wait_until(lambda: any(j.notes == "同じ秒のコメント" for j in store.journals(sid, 1)))
    finally:
        mgr.stop_all()


def test_stop_all_discards_response_of_request_in_flight(db, fake):
    """止めたときに通信中だった取得の結果は、DBへ書き込まない。通信が終われば、残りの処理を待たずに止まる。"""
    fake.add_project(1, "A")
    fake.add_issue(1, 1, "担当")
    sid = ServerStore(db).save(Server(name="S", url=fake.base_url), api_key="test-key").server_id
    mgr = SyncManager(db, EventBus())
    mgr.restart()
    store = IssueStore(db)
    try:
        assert wait_until(lambda: store.get(sid, 1) is not None and store.journals_synced(sid, 1))
        fake.add_issue(2, 1, "止めた後に届く")
        fake.delay = 2.0
        mgr.sync_now()
        time.sleep(1.3)  # 1秒以内に同期が始まり、一覧の取得が通信中になる
        t = time.monotonic()
        assert mgr.stop_all(10)
        assert time.monotonic() - t < 5
        time.sleep(1.5)
        assert store.get(sid, 2) is None
    finally:
        fake.delay = 0
        mgr.stop_all()
