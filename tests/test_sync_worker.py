import time

from PySide6.QtCore import QCoreApplication

from fake_redmine import ME

from remi.store.issues import IssueStore
from remi.store.servers import Server, ServerStore
from remi.sync.worker import SyncManager, prune_attachment_cache


def wait_until(cond, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_manager_syncs_and_posts(qapp, db, fake):
    fake.add_project(1, "A")
    fake.add_issue(1, 1, "担当")
    fake.add_journal(1, 7, "既存のコメント")
    sid = ServerStore(db).save(Server(name="S", url=fake.base_url), api_key="test-key").server_id
    mgr = SyncManager(db)
    changed, states, notes = [], [], []
    mgr.dataChanged.connect(lambda s, ids: changed.append(ids))
    mgr.stateChanged.connect(states.append)
    mgr.noteFinished.connect(lambda s, i, err: notes.append(err))
    mgr.restart()
    try:
        store = IssueStore(db)
        assert wait_until(lambda: store.get(sid, 1) is not None and store.journals_synced(sid, 1))
        assert store.unread_count(sid, 1) == 0
        s = ServerStore(db).get(sid)
        assert s.my_user_id == ME and s.last_error is None and s.last_synced_at
        assert states

        mgr.post_note(sid, 1, "返信します", False)
        assert wait_until(lambda: notes)
        assert notes == [None]
        assert wait_until(lambda: any(j.notes == "返信します" for j in store.journals(sid, 1)))
    finally:
        mgr.stop_all()


def test_worker_records_error_and_backs_off(qapp, db, fake):
    sid = ServerStore(db).save(Server(name="S", url=fake.base_url), api_key="wrong").server_id
    mgr = SyncManager(db)
    mgr.restart()
    try:
        assert wait_until(lambda: ServerStore(db).get(sid).last_error == "unauthorized")
        n = len(fake.requests)
        time.sleep(1.5)
        QCoreApplication.processEvents()
        assert len(fake.requests) == n  # バックオフ中は問い合わせない
    finally:
        mgr.stop_all()


def test_attachment_download_and_cache_limit(qapp, db, fake, tmp_path):
    fake.add_project(1, "A")
    fake.add_issue(1, 1, "担当")
    fake.add_attachment(1, 9, "shot.png", b"\x89PNG" + b"0" * 2000)
    sid = ServerStore(db).save(Server(name="S", url=fake.base_url), api_key="test-key").server_id
    mgr = SyncManager(db)
    ready = []
    mgr.attachmentReady.connect(lambda s, a, p: ready.append(p))
    mgr.restart()
    try:
        store = IssueStore(db)
        assert wait_until(lambda: store.get_attachment(sid, 9) is not None)
        mgr.request_attachment(sid, 9)
        assert wait_until(lambda: ready)
        assert ready[0].endswith(".png")
        assert store.get_attachment(sid, 9).local_path == ready[0]
    finally:
        mgr.stop_all()
    prune_attachment_cache(db, limit_mb=0)
    assert store.get_attachment(sid, 9).local_path is None
