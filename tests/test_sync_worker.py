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


# --- 想定外のエラー・サーバーの負荷を抑える仕組み ---------------------------------------------------
def _count(fake, pred):
    return sum(1 for m, p in list(fake.requests) if pred(m, p))


def _assigned_queries(fake):
    # 一覧の取得(limit=100)だけ数える(件数の確認は limit=1)
    return _count(fake, lambda m, p: p.startswith("/issues.json") and "assigned_to_id=me" in p and "limit=100" in p)


def _start(db, fake, issues=(1,), bus=None):
    fake.add_project(1, "A")
    for iid in issues:
        fake.add_issue(iid, 1, f"担当{iid}")
    sid = ServerStore(db).save(Server(name="S", url=fake.base_url), api_key="test-key").server_id
    mgr = SyncManager(db, bus or EventBus())
    mgr.restart()
    return sid, mgr


def test_unexpected_error_in_command_does_not_stop_worker(db, fake, monkeypatch):
    """画面からの依頼(取り直しなど)で想定外のエラーが起きても、同期スレッドは止まらない。"""
    sid, mgr = _start(db, fake)
    try:
        store = IssueStore(db)
        assert wait_until(lambda: store.journals_synced(sid, 1))
        w = mgr._worker(sid)
        original = w.sync.fetch_journals
        calls = []

        def flaky(issue_id, notify=True):
            calls.append(issue_id)
            if len(calls) == 1:
                raise KeyError("project")  # 想定外の応答など
            return original(issue_id, notify=notify)

        monkeypatch.setattr(w.sync, "fetch_journals", flaky)
        mgr.refetch_issue(sid, 1)
        mgr.refetch_issue(sid, 1)
        assert wait_until(lambda: len(calls) >= 2)
        assert w.is_alive()
    finally:
        mgr.stop_all()


def test_open_issue_is_not_checked_while_hidden(db, fake, monkeypatch):
    """トレイに格納・最小化している間は、開いているチケットを確かめない。表示したら、すぐ確かめる。"""
    from remi.sync import worker

    monkeypatch.setattr(worker, "OPEN_ISSUE_INTERVAL", 0.3)
    sid, mgr = _start(db, fake)
    try:
        assert wait_until(lambda: IssueStore(db).journals_synced(sid, 1))
        mgr.set_open_issue(sid, 1)

        def checks():
            return _count(fake, lambda m, p: p == "/issues/1.json")

        assert wait_until(lambda: checks() >= 2)
        mgr.set_visible(False)
        time.sleep(0.5)  # 通信中だった確認が終わるのを待つ
        n = checks()
        time.sleep(1.5)
        assert checks() == n
        mgr.set_visible(True)
        assert wait_until(lambda: checks() > n, timeout=3)
    finally:
        mgr.stop_all()


def test_manual_sync_now_is_ignored_only_while_syncing(db, fake):
    """「今すぐ同期」は、同期している間だけ受け付けない。終わったら、待ち時間なしですぐ受け付ける。"""
    sid, mgr = _start(db, fake)
    try:
        assert wait_until(lambda: IssueStore(db).journals_synced(sid, 1))
        assert _assigned_queries(fake) == 1
        assert mgr.sync_now() is True  # 差分同期と、関係が外れたチケットの突き合わせ(一覧を取り直す)の2回
        assert mgr.sync_now() is False  # 受け付けた直後(同期中)は、連打を受け付けない
        assert wait_until(lambda: _assigned_queries(fake) == 3)
        assert wait_until(lambda: "sync" not in mgr.bus.activity(sid))
        assert mgr.sync_now() is True  # 終わったら、すぐ受け付ける
        assert wait_until(lambda: _assigned_queries(fake) == 5)
        assert wait_until(lambda: "sync" not in mgr.bus.activity(sid))
        before = _assigned_queries(fake)
        assert mgr.sync_now(manual=False) is True  # 内部からの同期は、差分だけ
        assert wait_until(lambda: _assigned_queries(fake) == before + 1)
    finally:
        mgr.stop_all()


def test_journal_queue_is_paced(db, fake, monkeypatch):
    """コメントの遅延取得は JOURNAL_QUEUE_INTERVAL ごとに1件だけ(多数の利用者で一斉に取りに行かない)。"""
    from remi.sync import engine, worker

    monkeypatch.setattr(worker, "JOURNAL_QUEUE_INTERVAL", 0.8)
    times = []
    original = engine.ServerSync.fetch_journals

    def recorded(self, issue_id, notify=True):
        times.append(time.monotonic())
        return original(self, issue_id, notify=notify)

    monkeypatch.setattr(engine.ServerSync, "fetch_journals", recorded)
    sid, mgr = _start(db, fake, issues=(1, 2, 3))
    try:
        store = IssueStore(db)
        assert wait_until(lambda: all(store.journals_synced(sid, i) for i in (1, 2, 3)))
        assert len(times) == 3
        assert all(b - a >= 0.7 for a, b in zip(times, times[1:]))
    finally:
        mgr.stop_all()


def test_metadata_is_not_refetched_on_every_start(db, fake):
    """プロジェクト一覧・マスタは、前回の取得から時間が経つまで、起動し直しても取り直さない。
    持っていない(キャッシュを消した)ときは、すぐ取得する。"""
    from remi.store.settings import Settings

    def statuses():
        return _count(fake, lambda m, p: p == "/issue_statuses.json")

    sid, mgr = _start(db, fake)
    try:
        assert wait_until(lambda: IssueStore(db).journals_synced(sid, 1))
        assert statuses() == 1 and Settings(db).get(f"last_metadata:{sid}")
        mgr.restart()
        n = _assigned_queries(fake)
        assert wait_until(lambda: _assigned_queries(fake) > n)  # 差分同期は動いている
        assert statuses() == 1
        with db.tx() as c:
            c.execute("DELETE FROM masters WHERE server_id = ?", (sid,))
        mgr.restart()
        assert wait_until(lambda: statuses() == 2)
    finally:
        mgr.stop_all()


def test_backoff_respects_retry_after(db, fake):
    """サーバーが流量制限(429)で Retry-After を返したら、その時間は問い合わせない。"""
    fake.fail_next = 429
    fake.fail_headers = {"Retry-After": "3000"}
    sid, mgr = _start(db, fake)
    try:
        assert wait_until(lambda: ServerStore(db).get(sid).last_error == "rate_limited")
        w = mgr._worker(sid)
        left = w.sched.backoff_until - time.monotonic()
        assert 2900 < left < 3000 * 1.25  # 指定より短くならず、上乗せは2割まで
    finally:
        mgr.stop_all()


def test_restart_does_not_start_new_workers_while_old_ones_are_alive(db, fake, monkeypatch):
    """旧ワーカーが止まりきらないときは、新しいワーカーを並行して動かさない。"""
    sid, mgr = _start(db, fake)
    try:
        old = mgr._worker(sid)
        monkeypatch.setattr(old, "join", lambda timeout=None: None)
        monkeypatch.setattr(old, "is_alive", lambda: True)
        assert mgr.restart() is False
        assert mgr._worker(sid) is old  # 見失わず、管理に残っている
        monkeypatch.undo()  # 止まった後は、自動で再試行して起動する
        monkeypatch.setattr("remi.sync.worker.RESTART_RETRY_INTERVAL", 0.05)
        assert wait_until(lambda: mgr._worker(sid) is not old)
        assert mgr.restart() is True
    finally:
        mgr.stop_all()


def test_first_sync_starts_immediately_at_app_start(db, fake):
    """アプリを起動したら、すぐ同期する(起動直後の新着・通知を遅らせない)。"""
    fake.add_project(1, "A")
    fake.add_issue(1, 1, "担当")
    servers = ServerStore(db)
    synced = servers.save(Server(name="S", url=fake.base_url), api_key="test-key").server_id
    servers.update_fields(synced, initial_sync_done=True, last_sync_cursor="2026-01-01T00:00:00Z")
    mgr = SyncManager(db, EventBus())
    mgr.restart()
    try:
        assert wait_until(lambda: servers.get(synced).last_synced_at is not None, timeout=5.0)
    finally:
        mgr.stop_all()


def test_opening_an_issue_fetches_it_without_waiting_for_the_next_tick(db, fake, monkeypatch):
    """チケットを開いたら、刻み(TICK_SECONDS)も先読みの間隔も待たず、すぐコメントを取得する。"""
    from remi.sync import worker

    monkeypatch.setattr(worker, "TICK_SECONDS", 30.0)
    monkeypatch.setattr(worker, "JOURNAL_QUEUE_INTERVAL", 30.0)
    sid, mgr = _start(db, fake, issues=(1, 2))
    try:
        store = IssueStore(db)
        assert wait_until(lambda: store.get(sid, 1) is not None and store.get(sid, 2) is not None)
        assert wait_until(lambda: store.journals_synced(sid, 1) or store.journals_synced(sid, 2))
        target = 2 if store.journals_synced(sid, 1) else 1  # 先読みが済んでいない方(次の先読みは30秒後)
        assert not store.journals_synced(sid, target)
        mgr.set_open_issue(sid, target)
        assert wait_until(lambda: store.journals_synced(sid, target), timeout=3.0)
    finally:
        mgr.stop_all()


def test_reassigned_issue_is_noticed_by_the_next_sync_and_by_manual_sync(db, fake):
    """担当を渡したチケットは、次の差分同期で件数の違いから気づく。「今すぐ同期」でも、すぐ突き合わせる。"""
    sid, mgr = _start(db, fake, issues=(1, 2, 3))
    try:
        store = IssueStore(db)
        assert wait_until(lambda: all(store.get(sid, i) is not None for i in (1, 2, 3)))
        assert wait_until(lambda: all(store.journals_synced(sid, i) for i in (1, 2, 3)))
        time.sleep(1.2)  # 最初の同期が落ち着くのを待つ(刻みの境目を避ける)
        fake.issues[2]["assigned_to"] = {"id": 7, "name": "田中"}
        fake.issues[2]["updated_on"] = "2099-01-01T00:00:00Z"
        mgr.sync_now(manual=False)  # 通常の差分同期(突き合わせの時刻は来ていない)
        assert wait_until(lambda: not store.get(sid, 2).monitored, timeout=5.0)
        fake.issues[3]["assigned_to"] = {"id": 7, "name": "田中"}
        fake.issues[3]["updated_on"] = "2099-01-02T00:00:00Z"
        mgr.sync_now()  # 「今すぐ同期」
        assert wait_until(lambda: not store.get(sid, 3).monitored, timeout=5.0)
        assert store.get(sid, 1).monitored
    finally:
        mgr.stop_all()


def test_unexpected_key_error_during_sync_is_recorded_not_fatal(db, fake, monkeypatch):
    """同期中の KeyError(想定外の応答)を「サーバーの削除」と取り違えて、黙って止まらない。"""
    from remi.sync import engine

    def broken(self):
        raise KeyError("project")

    monkeypatch.setattr(engine.ServerSync, "sync_changes", broken)
    sid, mgr = _start(db, fake)
    try:
        assert wait_until(lambda: ServerStore(db).get(sid).last_error == "invalid_response")
        assert mgr._worker(sid).is_alive()
    finally:
        mgr.stop_all()
