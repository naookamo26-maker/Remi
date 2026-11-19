from datetime import UTC, datetime, timedelta

import pytest

from fake_redmine import ME

from remi.store.issues import REL_ASSIGNED, REL_AUTHORED, REL_GROUP, REL_WATCHING, IssueStore
from remi.store.projects import ProjectStore
from remi.store.servers import Server, ServerStore
from remi.sync.engine import ServerSync, format_ts


def ts(minutes_ago: float) -> str:
    return format_ts(datetime.now(UTC) - timedelta(minutes=minutes_ago))


@pytest.fixture
def setup(db, fake, client):
    fake.add_project(1, "基幹")
    fake.add_project(2, "受注", parent=1)
    server = ServerStore(db).save(Server(name="S", url=fake.base_url, my_user_id=ME, my_group_ids=[100]),
                                  api_key="test-key")
    sync = ServerSync(db, server.server_id, client)
    sync.refresh_metadata()
    return sync, IssueStore(db), server.server_id


def test_initial_sync_marks_everything_read(setup, fake):
    sync, issues, sid = setup
    fake.add_issue(1, 2, "担当", updated_on=ts(60))
    fake.add_issue(2, 2, "ウォッチ", assigned_to=7, watchers={ME}, updated_on=ts(50))
    fake.add_issue(3, 2, "関係なし", assigned_to=7, updated_on=ts(40))
    fake.add_issue(4, 2, "グループ", assigned_to=100, updated_on=ts(30))
    fake.add_journal(1, 7, "古いコメント", created_on=ts(59))

    result = sync.sync_changes()
    assert result.changed_issue_ids == {1, 2, 4}
    assert issues.get(sid, 1).relation_flags == REL_ASSIGNED
    assert issues.get(sid, 2).relation_flags == REL_WATCHING
    assert issues.get(sid, 4).relation_flags == REL_ASSIGNED | REL_GROUP
    assert sync.server.initial_sync_done

    # 遅延取得: 初回同期のチケットは既存の journal を既読にし、通知もしない
    assert set(sync.pending_journal_issue_ids()) == {1, 2, 4}
    r = sync.fetch_journals(1)
    assert r.new_comments == []
    assert issues.unread_count(sid, 1) == 0
    assert sync.pending_journal_issue_ids() and 1 not in sync.pending_journal_issue_ids()


def test_incremental_sync_detects_new_comment(setup, fake):
    sync, issues, sid = setup
    fake.add_issue(1, 2, "担当", updated_on=ts(60))
    sync.sync_changes()
    sync.fetch_journals(1)

    fake.add_journal(1, 7, "新しいコメント")
    fake.add_journal(1, ME, "自分のコメント")
    r = sync.sync_changes()
    assert 1 in r.changed_issue_ids
    r = sync.fetch_journals(1)
    assert [c.notes for c in r.new_comments] == ["新しいコメント"]
    assert issues.unread_count(sid, 1) == 1
    issues.mark_read(sid, 1)
    assert issues.unread_count(sid, 1) == 0


def test_status_change_is_not_unread(setup, fake):
    sync, issues, sid = setup
    fake.add_issue(1, 2, "担当", updated_on=ts(60))
    sync.sync_changes()
    sync.fetch_journals(1)
    fake.set_status(1, 2)
    sync.sync_changes()
    r = sync.fetch_journals(1)
    assert r.new_comments == []
    assert issues.unread_count(sid, 1) == 0
    assert issues.get(sid, 1).status_id == 2


def test_newly_assigned_issue_history_is_read_but_new_comments_unread(setup, fake):
    sync, issues, sid = setup
    fake.add_issue(9, 2, "最初は担当外", assigned_to=7, updated_on=ts(120))
    fake.add_journal(9, 8, "昔の議論", created_on=ts(110))
    sync.sync_changes()
    assert issues.get(sid, 9) is None

    fake.issues[9]["assigned_to"] = {"id": ME, "name": "岡本 ナオ"}
    fake.add_journal(9, 7, "お願いします")
    r = sync.sync_changes()
    assert 9 in r.changed_issue_ids
    r = sync.fetch_journals(9)
    assert [c.notes for c in r.new_comments] == ["お願いします"]
    assert issues.unread_count(sid, 9) == 1


def test_authored_issues_follow_project_setting(setup, fake, db):
    sync, issues, sid = setup
    fake.add_issue(5, 2, "自分が作成", assigned_to=7, author=ME, updated_on=ts(10))
    sync.sync_changes()
    assert issues.get(sid, 5) is None
    ProjectStore(db).set_setting(sid, 1, "watch_authored", True)  # 親で有効にすると子にも効く
    sync.servers.update_fields(sid, initial_sync_done=False)
    sync.sync_changes()
    assert issues.get(sid, 5).relation_flags == REL_AUTHORED


def test_reconcile_unmonitors_unassigned(setup, fake):
    sync, issues, sid = setup
    fake.add_issue(1, 2, "担当", updated_on=ts(60))
    fake.add_issue(2, 2, "担当2", updated_on=ts(60))
    sync.sync_changes()
    # 担当変更はチケットの更新になるが、自分の条件に合わないので差分には出ない
    fake.issues[2]["assigned_to"] = {"id": 7, "name": "田中"}
    fake.issues[2]["updated_on"] = ts(0)
    sync.sync_changes()
    assert issues.get(sid, 2).monitored
    r = sync.reconcile()
    assert r.unmonitored_issue_ids == {2}
    assert not issues.get(sid, 2).monitored
    assert {i.issue_id for i in issues.list_issues([sid])} == {1}


def test_closed_issue_retention(setup, fake):
    sync, issues, sid = setup
    fake.add_issue(1, 2, "担当", updated_on=ts(60))
    sync.sync_changes()
    fake.set_status(1, 5)
    sync.sync_changes()
    assert issues.get(sid, 1).monitored  # 7日以内は残る
    fake.issues[1]["closed_on"] = ts(60 * 24 * 8)
    fake.issues[1]["updated_on"] = ts(0)
    sync.sync_changes()
    assert not issues.get(sid, 1).monitored


def test_detect_deleted(setup, fake):
    sync, issues, sid = setup
    fake.add_issue(1, 2, "担当", updated_on=ts(60))
    fake.add_issue(2, 2, "消える", updated_on=ts(60))
    sync.sync_changes()
    del fake.issues[2]
    r = sync.detect_deleted()
    assert r.deleted_issue_ids == {2}
    assert issues.get(sid, 2) is None


def test_check_issue_only_refetches_when_updated(setup, fake):
    sync, issues, sid = setup
    fake.add_issue(1, 2, "担当", updated_on=ts(60))
    sync.sync_changes()
    sync.fetch_journals(1)
    # 取得の直後は、同じ秒に入った更新を見逃さないよう一度だけ取り直す
    n = len(fake.requests)
    assert sync.check_issue(1).empty
    assert len(fake.requests) == n + 2
    n = len(fake.requests)
    assert sync.check_issue(1).empty
    assert len(fake.requests) == n + 1
    fake.add_journal(1, 7, "追加")
    r = sync.check_issue(1)
    assert [c.notes for c in r.new_comments] == ["追加"]


def test_cursor_uses_server_time(setup, fake):
    sync, issues, sid = setup
    old = "2020-01-01T00:00:00Z"
    fake.add_issue(1, 2, "古い更新", updated_on=old)
    sync.sync_changes()
    assert sync.server.last_sync_cursor == old
    fake.add_journal(1, 7, "x", created_on="2020-01-02T00:00:00Z")
    sync.sync_changes()
    assert sync.server.last_sync_cursor == "2020-01-02T00:00:00Z"
    queries = [p for _m, p in fake.requests if p.startswith("/issues.json") and "updated_on" in p]
    assert any("2019-12-31T23:58:00Z" in q for q in queries)  # 前回の時刻から2分重ねて問い合わせる


def test_attachments_are_stored(setup, fake):
    sync, issues, sid = setup
    fake.add_issue(1, 2, "担当", updated_on=ts(60))
    fake.add_attachment(1, 77, "shot.png", b"png")
    sync.sync_changes()
    sync.fetch_journals(1)
    [a] = issues.attachments(sid, 1)
    assert a.filename == "shot.png" and a.content_url.endswith("/77/shot.png")


def test_check_issue_catches_update_in_same_second(setup, fake):
    """updated_on は秒単位: 取得と同じ秒に入ったコメントも、開いているチケットなら拾う。"""
    sync, issues, sid = setup
    stamp = ts(0)
    fake.add_issue(1, 2, "担当", updated_on=stamp)
    sync.sync_changes()
    sync.fetch_journals(1)
    fake.add_journal(1, 7, "同じ秒のコメント", created_on=stamp)
    assert fake.issues[1]["updated_on"] == stamp
    r = sync.check_issue(1)
    assert [c.notes for c in r.new_comments] == ["同じ秒のコメント"]


def test_edited_or_deleted_comment_and_removed_attachment_update_screen(setup, fake):
    """新しいコメントがなくても、コメントの編集・削除や添付の削除があれば画面を更新させる。"""
    sync, issues, sid = setup
    fake.add_issue(1, 2, "担当", updated_on=ts(60))
    fake.add_journal(1, 7, "最初の本文")
    fake.add_journal(1, 7, "消されるコメント")
    fake.add_attachment(1, 77, "shot.png", b"png")
    sync.sync_changes()
    sync.fetch_journals(1)
    assert sync.fetch_journals(1).changed_issue_ids == set()  # 何も変わっていない

    fake.journals[1][0]["notes"] = "編集した本文"
    r = sync.fetch_journals(1)
    assert r.changed_issue_ids == {1} and r.new_comments == []
    assert issues.journals(sid, 1)[0].notes == "編集した本文"

    del fake.journals[1][1]
    assert sync.fetch_journals(1).changed_issue_ids == {1}

    del fake.attachments[77]
    assert sync.fetch_journals(1).changed_issue_ids == {1}
    assert issues.attachments(sid, 1) == []


def test_fetch_journals_resolves_names_in_details(setup, fake, db):
    """変更履歴に出てくる、知らないバージョン・カテゴリ・ユーザーの名前を問い合わせて覚える(見えないものは #ID)。"""
    from remi.store.masters import MasterStore

    sync, issues, sid = setup
    fake.add_version(5, 2, "v1.0")
    fake.add_version(6, 2, "v2.0")
    fake.categories[2] = [{"id": 3, "name": "画面"}]
    fake.add_issue(1, 2, "担当", version=6)
    fake.add_journal(1, 7, "", details=[
        {"property": "attr", "name": "fixed_version_id", "old_value": "5", "new_value": "6"},
        {"property": "attr", "name": "category_id", "old_value": None, "new_value": "3"},
        {"property": "attr", "name": "assigned_to_id", "old_value": "8", "new_value": "999"},
    ])
    sync.sync_changes()
    fake.requests.clear()
    sync.fetch_journals(1)
    masters = MasterStore(db)
    assert masters.names(sid, "version") == {5: "v1.0", 6: "v2.0"}
    assert masters.names(sid, "category") == {3: "画面"}
    assert masters.names(sid, "user")[8] == "佐藤 花子"
    asked = [q for _, q in fake.requests if q.startswith(("/versions/", "/issue_categories/", "/users/"))]
    # v2.0 はチケットに含まれていたので問い合わせない。見えないユーザー(999)は1回だけ問い合わせる
    assert sorted(asked) == ["/issue_categories/3.json", "/users/8.json", "/users/999.json", "/versions/5.json"]
    fake.requests.clear()
    sync.fetch_journals(1)
    assert not [q for _, q in fake.requests if q.startswith(("/versions/", "/issue_categories/", "/users/"))]


def test_does_not_notify_the_same_comment_twice(setup, fake):
    """チケットのキャッシュを消して取り直しても(監視対象から外れて戻ったなど)、通知したコメントは再び通知しない。"""
    sync, issues, sid = setup
    fake.add_issue(1, 2, "担当", updated_on=ts(60))
    sync.sync_changes()
    sync.fetch_journals(1)
    fake.add_journal(1, 7, "新しいコメント")
    sync.sync_changes()
    r = sync.fetch_journals(1)
    assert [c.notes for c in r.new_comments] == ["新しいコメント"]  # 読まずにおく
    issues.delete_issues(sid, [1])
    sync.servers.update_fields(sid, last_sync_cursor=ts(120))  # 古いコメントも「新しく」見える状態にする
    sync.sync_changes()
    assert sync.fetch_journals(1).new_comments == []
    assert issues.unread_count(sid, 1) == 1  # 未読のまま(通知しないだけ)
