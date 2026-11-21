"""一覧の絞り込み・並べ替え・選択肢・0件の判定(設計書 §5.1)。"""

from datetime import UTC, date, datetime, timedelta

import pytest

from fake_redmine import ME

from remi.context import AppContext
from remi.server import issue_list
from remi.server.api import RemiApi
from remi.store.servers import Server, ServerStore
from remi.sync.engine import ServerSync, format_ts


def day(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


@pytest.fixture
def lst(db, fake, client):
    """担当・ウォッチのチケットをいくつか作って同期し、API を返す。"""
    fake.add_project(1, "A")
    # 優先度の順序は 通常(0)< 高め(1)、トラッカーは バグ(0)< 機能(1)、ステータスは 新規 < 進行中 < 終了
    fake.add_issue(1, 1, "あ請求書", assigned_to=ME, priority_id=3, tracker_id=1, status_id=1, due_date=day(-3))
    fake.add_issue(2, 1, "い画面", assigned_to=ME, priority_id=2, tracker_id=2, status_id=2, due_date=day(5))
    fake.add_issue(3, 1, "う帳票", assigned_to=7, watchers={ME}, priority_id=2, tracker_id=1, status_id=1)
    fake.add_issue(4, 1, "え終了済み", assigned_to=ME, priority_id=3, tracker_id=2, status_id=5, due_date=day(-10))
    fake.add_issue(5, 1, "お未割り当て", assigned_to=None, watchers={ME}, priority_id=2, tracker_id=1, status_id=2)
    sid = ServerStore(db).save(Server(name="S", url=fake.base_url, my_user_id=ME), api_key="test-key").server_id
    ctx = AppContext(db)
    sync = ServerSync(db, sid, client)
    sync.refresh_identity()
    sync.refresh_metadata()
    sync.sync_changes()
    ctx.settings.set("unread_first", False)
    return RemiApi(ctx)


def ids(res):
    return [i["issueId"] for i in res["issues"]]


def test_status_filter_modes(lst):
    # 既定は未完了(クローズ済みは出さない)
    assert sorted(ids(lst.listIssues())) == [1, 2, 3, 5]
    assert sorted(ids(lst.listIssues(statusMode="all"))) == [1, 2, 3, 4, 5]
    assert ids(lst.listIssues(statusMode="closed")) == [4]
    # 個別に選ぶと、モードは使わない(クローズ済みの「終了」も選べる)
    assert sorted(ids(lst.listIssues(statuses=["進行中", "終了"]))) == [2, 4, 5]
    assert sorted(ids(lst.listIssues(statuses=["新規"], statusMode="closed"))) == [1, 3]


def test_attribute_filters_are_and_across_groups_or_within(lst):
    assert sorted(ids(lst.listIssues(trackers=["バグ"]))) == [1, 3, 5]
    assert sorted(ids(lst.listIssues(priorities=["高め"]))) == [1]
    assert sorted(ids(lst.listIssues(trackers=["バグ", "機能"]))) == [1, 2, 3, 5]  # 同じ項目の中は「または」
    assert sorted(ids(lst.listIssues(trackers=["バグ"], priorities=["通常"]))) == [3, 5]  # 項目どうしは「かつ」
    assert sorted(ids(lst.listIssues(assignees=["@me"]))) == [1, 2]
    assert ids(lst.listIssues(assignees=["@none"])) == [5]
    assert sorted(ids(lst.listIssues(assignees=["@none", "@me"]))) == [1, 2, 5]
    assert sorted(ids(lst.listIssues(due=["overdue"]))) == [1]  # クローズ済みの期限切れは数えない
    assert sorted(ids(lst.listIssues(due=["none"]))) == [3, 5]


def test_search_and_relation_still_work_with_conditions(lst):
    assert ids(lst.listIssues(search="画面")) == [2]
    assert sorted(ids(lst.listIssues(relation="watching"))) == [3, 5]
    assert ids(lst.listIssues(relation="watching", trackers=["機能"])) == []


def test_sort_keys_and_directions(lst):
    f = lambda **kw: ids(lst.listIssues(statusMode="all", **kw))  # noqa: E731
    assert f(sort="number", desc=False) == [1, 2, 3, 4, 5]
    assert f(sort="number", desc=True) == [5, 4, 3, 2, 1]
    assert f(sort="subject", desc=False) == [1, 2, 3, 4, 5]  # あ い う え お
    # ステータス: Redmine の順序(新規 < 進行中 < 終了)。同じ値は更新日時の新しい順
    assert f(sort="status", desc=False)[0] in (1, 3) and f(sort="status", desc=False)[-1] == 4
    assert f(sort="status", desc=True)[0] == 4
    # 優先度は高い順(降順)= 順序の大きいほうから
    assert set(f(sort="priority", desc=True)[:2]) == {1, 4}
    assert set(f(sort="priority", desc=False)[:3]) == {2, 3, 5}
    # 期日: 近い順は日付の昇順。期日なし(3,5)は、昇順でも降順でも最後
    assert f(sort="due", desc=False)[:3] == [4, 1, 2] and set(f(sort="due", desc=False)[3:]) == {3, 5}
    assert f(sort="due", desc=True)[:3] == [2, 1, 4] and set(f(sort="due", desc=True)[3:]) == {3, 5}
    with pytest.raises(Exception):  # noqa: B017,PT011
        lst.listIssues(sort="nope")


def test_unread_first_keeps_chosen_order(lst, fake, client):
    """「未読を上に」は、選んだ並びの中で、未読を先頭に出す(並びは変えない)。"""
    # チケット3に他の人のコメント(未読)を付けて、journal を取り込む
    later = format_ts(datetime.now(UTC) + timedelta(hours=1))  # 取り込んだ時刻(既読の基準)より後
    fake.add_journal(3, 7, "確認してください", created_on=later)
    sync = ServerSync(lst.ctx.db, 1, client)
    sync.sync_changes()
    sync.fetch_journals(3, notify=False)
    lst.ctx.settings.set("unread_first", True)
    res = lst.listIssues(sort="number", desc=False)
    assert ids(res) == [3, 1, 2, 5] and res["issues"][0]["unread"] == 1
    lst.ctx.settings.set("unread_first", False)
    assert ids(lst.listIssues(sort="number", desc=False)) == [1, 2, 3, 5]


def test_facets_count_with_other_conditions(lst):
    res = lst.listIssues(trackers=["バグ"])
    f = res["facets"]
    # トラッカーの件数は、トラッカー以外の条件(未完了)だけを当てた件数: バグ 3(1,3,5)、機能 1(2)
    assert {x["value"]: x["count"] for x in f["tracker"]} == {"バグ": 3, "機能": 1}
    # 優先度の件数は、選んだトラッカー(バグ)を当てたあと: 高め 1(1)、通常 2(3,5)。Redmine の順序(通常 → 高め)
    assert [(x["value"], x["count"]) for x in f["priority"]] == [("通常", 2), ("高め", 1)]
    # ステータスの個別は、クローズ済みも含む(未完了/クローズ済みの絞り込みで消えない)
    assert {x["value"] for x in lst.listIssues()["facets"]["status"]} == {"新規", "進行中", "終了"}
    # 担当者: 自分 → 未割り当て → 名前
    assert [x["value"] for x in f["assignee"]][:2] == ["@me", "@none"]
    # 選んでいる値は、件数が0でも残る
    f2 = lst.listIssues(trackers=["バグ"], priorities=["高め"], assignees=["@none"])["facets"]
    assert ("高め", 0) in [(x["value"], x["count"]) for x in f2["priority"]]
    assert {x["value"] for x in f2["due"]} <= set(issue_list.DUE_KEYS)


def test_scope_total_distinguishes_no_match_from_no_tickets(lst):
    res = lst.listIssues(search="存在しない")
    assert res["issues"] == [] and res["scopeTotal"] == 5  # チケットはあるが、条件に一致しない
    assert "scopeTotal" not in lst.listIssues()
    # 既定(未完了)だけで0件になる場合(クローズ済みしかない)も、チケットはある
    only_closed = lst.listIssues(statusMode="closed", trackers=["バグ"])
    assert only_closed["issues"] == [] and only_closed["scopeTotal"] == 5


def test_no_tickets_at_all(db, fake):
    ctx = AppContext(db)
    api = RemiApi(ctx)
    res = api.listIssues()
    assert res["issues"] == [] and res["scopeTotal"] == 0


def test_list_settings_are_validated_and_migrated(lst):
    api = lst
    assert api.setSetting("list_sort", {"key": "due", "desc": False})["list_sort"] == {"key": "due", "desc": False}
    assert api.setSetting("list_status", {"mode": "closed", "names": []})["list_status"]["mode"] == "closed"
    assert api.setSetting("list_filter", {"trackers": ["バグ"], "priorities": [], "assignees": ["@me"], "due": ["week"]})
    assert api.setSetting("list_search", "請求書")["list_search"] == "請求書"
    from remi.server.api import RpcError

    for key, bad in [("list_sort", {"key": "x"}), ("list_status", {"mode": "weird"}), ("list_filter", {"due": ["x"]}),
                     ("list_search", 5), ("version_status", [])]:
        with pytest.raises(RpcError):
            api.setSetting(key, bad)
    # 旧設定(show_closed・version_show_closed)は、画面からは変えられない
    for old in ("show_closed", "version_show_closed"):
        with pytest.raises(RpcError):
            api.setSetting(old, True)


@pytest.mark.parametrize("key", ["priority", "tracker", "status"])
@pytest.mark.parametrize("desc", [False, True])
def test_sort_keeps_unknown_order_last_in_both_directions(lst, key, desc):
    """Redmine での順序を知らない項目(マスターを取得し直す前)は、昇順でも降順でも、最後に置く。"""
    row = {"priority": 3, "tracker": 2, "status": 2}[key]  # 順序を消す項目の ID(下の unknown のチケットが持つ値)
    with lst.ctx.db.tx() as c:
        c.execute("UPDATE masters SET extra_json = '{}' WHERE kind = ? AND id = ?", (key, row))
    res = lst.listIssues(statusMode="all", sort=key, desc=desc)
    unknown = {"priority": {1, 4}, "tracker": {2, 4}, "status": {2, 5}}[key]
    tail = ids(res)[-len(unknown):]
    assert set(tail) == unknown, (key, desc, ids(res))


def test_closed_issues_do_not_show_unread(lst):
    """クローズ済みは未読数の対象外(ツリー・トレイと同じ)。一覧にも未読を出さず、「未読のみ」にも出さない。"""
    lst.ctx.issues.upsert_journals(1, 4, [{"id": 900, "notes": "終了後のコメント", "user": {"id": 7, "name": "佐藤"},
                                           "created_on": "2026-01-01T00:00:00Z"}])
    assert lst.ctx.issues.unread_count(1, 4) == 1  # 既読の位置の上では未読だが
    closed = [i for i in lst.listIssues(statusMode="all")["issues"] if i["issueId"] == 4][0]
    assert closed["unread"] == 0  # 一覧には出さない(「すべて既読にする」の対象外なので、消せない未読を見せない)
    assert 4 not in ids(lst.listIssues(statusMode="all", unreadOnly=True))


def test_mark_all_read_in_one_statement(lst):
    """「すべて既読にする」: 監視対象のチケットだけを、最新のコメントまで既読にする。範囲(プロジェクト)・クローズ済み・既読位置を守る。"""
    store = lst.ctx.issues
    note = {"notes": "コメント", "user": {"id": 7, "name": "佐藤"}, "created_on": "2026-01-01T00:00:00Z"}
    store.upsert_journals(1, 1, [{"id": 101, **note}, {"id": 102, **note}])
    store.upsert_journals(1, 2, [{"id": 201, **note}])
    store.upsert_journals(1, 4, [{"id": 401, **note}])  # クローズ済み(監視対象外)
    assert store.unread_count(1, 1) == 2 and store.unread_count(1, 2) == 1
    store.mark_read(1, 3, 999)  # 先に進んでいる既読位置は、戻さない

    lst.markAllRead(serverId=1)
    assert [store.unread_count(1, i) for i in (1, 2)] == [0, 0]
    assert store.last_read(1, 1) == 102 and store.last_read(1, 2) == 201
    assert store.last_read(1, 3) == 999
    assert store.last_read(1, 5) == 0  # コメントのないチケットも、既読の状態になる
    assert store.unread_count(1, 4) == 1  # クローズ済みは対象外(一覧にも未読を出さない)

    store.upsert_journals(1, 1, [{"id": 103, **note}])
    # 範囲外のプロジェクトを指定すると、何も変えない
    lst.markAllRead(serverId=1, projectId=999)
    assert store.unread_count(1, 1) == 1
    lst.markAllRead(serverId=1, projectId=1)
    assert store.unread_count(1, 1) == 0
