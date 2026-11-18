"""ロードマップ(バージョン)の監視(設計書 §5.5)。"""

import pytest

from fake_redmine import ME

from remi.store.issues import IssueStore
from remi.store.projects import ProjectStore
from remi.store.servers import Server, ServerStore
from remi.store.versions import VersionStore
from remi.sync.engine import ServerSync


@pytest.fixture
def setup(db, fake, client):
    fake.add_project(1, "基幹")
    fake.add_project(2, "受注", parent=1)
    fake.add_project(3, "社内")
    server = ServerStore(db).save(Server(name="S", url=fake.base_url, my_user_id=ME), api_key="test-key")
    sync = ServerSync(db, server.server_id, client)
    sync.refresh_metadata()
    return sync, VersionStore(db), server.server_id


def kinds(versions, sid, vid):
    return [(e.kind, e.issue_id) for e in versions.events(sid, vid)]


def test_watch_versions_records_changes(setup, fake, db):
    sync, versions, sid = setup
    fake.add_version(10, 2, "v1.0")
    fake.add_version(11, 2, "v0.9", status="closed")
    fake.add_version(30, 3, "社内のバージョン")
    fake.add_issue(100, 2, "関係ないチケット", assigned_to=7, version=10)
    fake.add_issue(101, 2, "自分のチケット", version=10)
    fake.add_issue(102, 2, "終わったバージョンのチケット", assigned_to=7, status_id=5, version=11)

    # ウォッチしていなければ何もしない
    assert sync.sync_versions() == set()
    assert versions.list(sid) == []

    # 親でウォッチすると、子プロジェクトのバージョンも対象になる(社内は対象外)
    ProjectStore(db).set_setting(sid, 1, "watch_versions", True)
    assert sync.sync_versions() == {10, 11}
    assert {v.version_id for v in versions.list(sid)} == {10, 11}
    assert versions.members(sid, 10) == {100: False, 101: False}
    assert versions.members(sid, 11) == {102: True}
    assert versions.progress(sid) == {10: (0, 2), 11: (1, 1)}
    # 最初の取得では、変化を記録しない
    assert kinds(versions, sid, 10) == [] and versions.unread_counts(sid) == {}
    # 関係ないチケットは、監視対象外として保存する(件数・未読数・通知には入らない)
    issues = IssueStore(db)
    row = issues.get(sid, 100)
    assert row.relation_flags == 0 and not row.monitored
    assert set(issues.count_by_project(sid)) == set()  # 自分のチケットはまだ同期していない
    assert sync.sync_versions() == set()  # 変化がなければ何も返さない

    # 追加・外れた・完了・期日の変更・新しいバージョン
    fake.add_issue(103, 2, "追加されたチケット", assigned_to=7)
    fake.set_version(103, 10)
    fake.set_version(100, None)
    fake.set_status(101, 5)
    fake.update_version(10, due_date="2026-12-01")
    fake.add_version(12, 1, "v2.0")
    assert sync.sync_versions() == {10, 12}
    assert sorted(kinds(versions, sid, 10)) == sorted(
        [("due_date", None), ("added", 103), ("removed", 100), ("closed", 101)])
    removed = next(e for e in versions.events(sid, 10) if e.kind == "removed")
    assert removed.subject == "関係ないチケット"
    assert kinds(versions, sid, 12) == [("created", None)]
    assert versions.unread_counts(sid) == {10: 4, 12: 1}
    versions.mark_read(sid, 10)
    assert versions.unread_counts(sid) == {12: 1}
    assert issues.list_issues([sid], version_id=10)[0].issue_id in {101, 103}

    # ウォッチをやめると、バージョンは消える
    ProjectStore(db).set_setting(sid, 1, "watch_versions", False)
    assert sync.sync_versions() == {10, 11, 12}
    assert versions.list(sid) == []


def test_child_project_can_opt_out(setup, fake, db):
    sync, versions, sid = setup
    fake.add_version(10, 1, "親のバージョン")
    fake.add_version(20, 2, "子のバージョン")
    projects = ProjectStore(db)
    projects.set_setting(sid, 1, "watch_versions", True)
    projects.set_setting(sid, 2, "watch_versions", False)
    sync.sync_versions()
    assert {v.version_id for v in versions.list(sid)} == {10}
