"""全体の確認(レビュー)で見つかった不具合の修正の確認。"""

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fake_redmine import ME
from test_sync_worker import wait_until

from remi.api.client import RedmineClient
from remi.context import AppContext
from remi.events import EventBus
from remi.server.api import RemiApi
from remi.store.issues import IssueStore
from remi.store.servers import Server, ServerStore
from remi.sync.engine import ServerSync, format_ts
from remi.sync.worker import SyncManager


def ts(minutes_ago: float) -> str:
    return format_ts(datetime.now(UTC) - timedelta(minutes=minutes_ago))


def test_deleted_open_issue_does_not_stall_sync(db, fake):
    """開いているチケットが Redmine で削除されても、そのサーバーの同期は止まらない(今まで約30分止まっていた)。"""
    fake.add_project(1, "A")
    fake.add_issue(1, 1, "a")
    fake.add_issue(2, 1, "b")
    sid = ServerStore(db).save(Server(name="S", url=fake.base_url), api_key="test-key").server_id
    mgr = SyncManager(db, EventBus())
    mgr.restart()
    store = IssueStore(db)
    try:
        assert wait_until(lambda: store.journals_synced(sid, 1) and store.journals_synced(sid, 2))
        mgr.set_open_issue(sid, 1)
        time.sleep(0.3)
        del fake.issues[1]
        mgr.set_open_issue(sid, None)
        mgr.set_open_issue(sid, 1)
        assert wait_until(lambda: store.get(sid, 1) is None)  # 削除されたので、キャッシュも消える
        fake.add_journal(2, 7, "別チケットの新着コメント")
        fake.issues[2]["updated_on"] = format_ts(datetime.now(UTC) + timedelta(seconds=5))  # 更新日時を進める
        mgr.sync_now()
        assert wait_until(lambda: any(j.notes == "別チケットの新着コメント" for j in store.journals(sid, 2)))
        assert ServerStore(db).get(sid).last_error is None
    finally:
        mgr.stop_all()


def test_forbidden_issue_is_unmonitored_without_server_error(db, fake, client):
    fake.add_project(1, "A")
    fake.add_issue(1, 1, "a")
    sid = ServerStore(db).save(Server(name="S", url=fake.base_url, my_user_id=ME), api_key="test-key").server_id
    sync = ServerSync(db, sid, client)
    sync.refresh_metadata()
    sync.sync_changes()
    from remi.api.errors import ErrorCode, RedmineError

    r = sync.issue_unavailable(1, RedmineError(ErrorCode.FORBIDDEN, 403))
    assert r.unmonitored_issue_ids == {1}
    assert sync.issues.get(sid, 1).monitored is False


def test_cache_reset_forgets_version_progress(db, fake):
    """サーバーのURL・ユーザーを変えて初期化したあと、取り直したバージョンが「作成」の変化にならない。"""
    fake.add_project(1, "A")
    fake.add_version(10, 1, "v1.0")
    fake.add_issue(1, 1, "a")
    sid = ServerStore(db).save(Server(name="S", url=fake.base_url, my_user_id=ME), api_key="test-key").server_id
    ctx = AppContext(db)
    ctx.projects.set_setting(sid, 1, "watch_versions", True)
    c = RedmineClient(fake.base_url, "test-key", min_interval=0)
    sync = ServerSync(db, sid, c)
    sync.refresh_identity()
    sync.refresh_metadata()
    sync.sync_versions()
    sync.sync_versions()
    ctx.settings.set(f"last_metadata:{sid}", 1)
    RemiApi(ctx)._clear_server_cache(sid)
    assert ctx.settings.get(f"versions_seen_projects:{sid}") is None
    assert ctx.settings.get(f"last_metadata:{sid}") is None
    sync.refresh_metadata()
    sync.sync_versions()
    assert ctx.versions.events(sid, 10) == []
    assert ctx.versions.unread_counts(sid) == {}
    c.close()


def test_unrelated_closed_issue_is_not_in_main_list(db, fake, client):
    """「表示するチケット」を4種すべてにして「クローズ済みも表示」にしても、自分と無関係なチケットは出ない。"""
    fake.add_project(1, "A")
    fake.add_version(10, 1, "v1.0")
    fake.add_issue(1, 1, "mine", assigned_to=ME)
    fake.add_issue(2, 1, "other", assigned_to=8)
    fake.set_version(2, 10)
    fake.set_status(2, 5)
    sid = ServerStore(db).save(Server(name="S", url=fake.base_url, my_user_id=ME), api_key="test-key").server_id
    ctx = AppContext(db)
    ctx.projects.set_setting(sid, 1, "watch_versions", True)
    sync = ServerSync(db, sid, client)
    sync.refresh_identity()
    sync.refresh_metadata()
    sync.sync_changes()
    sync.sync_versions()
    api = RemiApi(ctx)
    api.setProjectSetting(sid, 1, "visible_relations", ["assigned", "group", "watching", "authored"])
    assert [i["issueId"] for i in api.listIssues(statusMode="all")["issues"]] == [1]
    # バージョンで開けば、無関係のものも出る
    assert {i["issueId"] for i in api.listIssues(serverId=sid, versionId=10)["issues"]} == {2}


@pytest.fixture
def stale(db, fake, client):
    fake.add_project(1, "A")
    sid = ServerStore(db).save(Server(name="S", url=fake.base_url, my_user_id=ME), api_key="test-key").server_id
    fake.add_issue(1, 1, "外れた", updated_on=ts(60 * 24 * 300))
    sync = ServerSync(db, sid, client)
    sync.refresh_metadata()
    sync.sync_changes()
    return sync, sync.issues, sid


def _backdate(issues, days):
    issues.db.query("UPDATE issues SET unmonitored_at = ?",
                    (format_ts(datetime.now(UTC) - timedelta(days=days)),))


def test_purge_waits_ten_days_from_unmonitoring(stale):
    sync, issues, sid = stale
    issues.set_monitored(sid, [1], False)  # 更新日時は300日前でも、外れたのは今
    assert issues.purge_stale() == 0
    _backdate(issues, 9)
    assert issues.purge_stale() == 0
    _backdate(issues, 11)
    assert issues.purge_stale() == 1
    assert issues.get(sid, 1) is None


def test_purge_keeps_draft_recently_opened_and_returned_issues(stale):
    sync, issues, sid = stale
    issues.set_monitored(sid, [1], False)
    _backdate(issues, 11)
    issues.save_draft(sid, 1, "書きかけ", False)
    assert issues.purge_stale() == 0  # 下書きがあるものは残す
    issues.clear_draft(sid, 1)
    issues.mark_opened(sid, 1)
    assert issues.purge_stale() == 0  # 最近開いたものは残す
    issues.db.query("UPDATE issues SET last_opened_at = NULL")
    issues.set_monitored(sid, [1], True)  # 戻った
    assert issues.get(sid, 1).monitored
    assert issues.purge_stale() == 0
    issues.set_monitored(sid, [1], False)  # もう一度外れたら、また10日数え直す
    assert issues.purge_stale() == 0


def test_delete_issues_removes_cached_image_files(stale, tmp_path):
    sync, issues, sid = stale
    f = tmp_path / "img.png"
    f.write_bytes(b"x")
    issues.upsert_attachments(sid, 1, [{"id": 5, "filename": "img.png", "content_type": "image/png"}])
    issues.set_attachment_cached(sid, 5, str(f))
    issues.delete_issues(sid, [1])
    assert not Path(f).exists()


def test_tree_tells_when_sync_will_retry(db, fake):
    from remi.server.tree import build_tree

    sid = ServerStore(db).save(Server(name="S", url=fake.base_url), api_key="test-key").server_id
    ctx = AppContext(db)
    assert build_tree(ctx)["servers"][0]["retryAt"] is None
    ServerStore(db).update_fields(sid, last_error="unauthorized")
    ctx.bus.set_retry_at(sid, time.time() + 600)
    retry = build_tree(ctx)["servers"][0]["retryAt"]
    assert 590_000 < retry - time.time() * 1000 < 610_000
    ctx.bus.set_retry_at(sid, None)
    assert build_tree(ctx)["servers"][0]["retryAt"] is None


def test_tray_hint_is_shown_only_once(db):
    from remi.system.notifier import Notifier

    class Backend:
        shown: list = []

        def show_message(self, title, body):
            self.shown.append((title, body))

    ctx = AppContext(db)
    backend = Backend()
    n = Notifier(ctx, api=None, on_open=lambda s, i: None, backend=backend)
    n.tray_hint()
    n.tray_hint()
    assert len(backend.shown) == 1
    assert "トレイ" in backend.shown[0][0] or "tray" in backend.shown[0][0]


def test_purge_keeps_the_open_issue(stale):
    sync, issues, sid = stale
    issues.set_monitored(sid, [1], False)
    _backdate(issues, 11)
    assert sync.purge_stale(keep={1}) == 0  # いま開いているチケットは消さない
    assert issues.get(sid, 1) is not None
    assert sync.purge_stale() == 1


def test_check_issue_exists_only_says_no_for_404(db):
    """チケットの有無の確認: 404 のときだけ「ない」。権限なし・つながらないなど、分からないときは「ある」(Redmineのページで確かめてもらう)"""
    from remi.api.errors import ErrorCode, RedmineError
    from remi.context import AppContext
    from remi.server.api import RemiApi, RpcError

    class Client:
        def __init__(self, error=None):
            self.error = error

        def issue(self, issue_id):
            if self.error:
                raise self.error
            return {"id": issue_id}

    class Sync:
        def __init__(self, client):
            self._client = client

        def client(self, server_id):
            return self._client

    api = RemiApi(AppContext(db), None)
    with pytest.raises(RpcError):  # サーバーにつながっていない
        api.checkIssueExists(1, 5)
    for error, exists in [(None, True), (RedmineError(ErrorCode.NOT_FOUND, 404, ""), False),
                          (RedmineError(ErrorCode.FORBIDDEN, 403, ""), True),
                          (RedmineError(ErrorCode.SERVER_ERROR, 500, ""), True), (OSError("down"), True)]:
        api.sync = Sync(Client(error))
        assert api.checkIssueExists(1, 5) == {"exists": exists}, error
