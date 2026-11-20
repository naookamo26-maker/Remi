from remi.store import secrets
from remi.store.masters import MasterStore
from remi.store.projects import ProjectStore
from remi.store.servers import Server, ServerStore


def make_server(db, **kw):
    return ServerStore(db).save(Server(name="S", url="http://s", **kw), api_key="k")


def test_server_crud_and_keyring(db, memory_keyring):
    store = ServerStore(db)
    s = make_server(db, my_group_ids=[1, 2])
    assert s.server_id
    assert secrets.get_api_key(s.server_id) == "k"
    got = store.get(s.server_id)
    assert got.my_group_ids == [1, 2] and got.text_format == "common_mark"
    got.name = "T"
    store.save(got)
    assert store.get(s.server_id).name == "T"
    store.update_fields(s.server_id, last_error="timeout", initial_sync_done=True)
    assert store.get(s.server_id).initial_sync_done is True
    store.delete(s.server_id)
    assert store.get(s.server_id) is None
    assert secrets.get_api_key(s.server_id) is None
    # 資格情報がDBに残っていないこと
    assert all("k" != v for r in db.query("SELECT * FROM servers") for v in tuple(r))


def test_assignee_ids():
    s = Server(name="a", url="u", my_user_id=5, my_group_ids=[10])
    assert s.assignee_ids == {5, 10}


def test_projects_with_inaccessible_parent(db):
    s = make_server(db)
    ps = ProjectStore(db)
    ps.replace_all(s.server_id, [
        {"id": 2, "name": "受注", "parent": {"id": 1, "name": "基幹"}},
        {"id": 3, "name": "在庫", "parent": {"id": 1, "name": "基幹"}},
        {"id": 4, "name": "孫", "parent": {"id": 2, "name": "受注"}},
    ])
    p1 = ps.get(s.server_id, 1)
    assert p1.name == "基幹" and not p1.accessible and p1.parent_id is None
    assert ps.ancestors(s.server_id, 4) == [2, 1]
    assert ps.descendants(s.server_id, 1) == {2, 3, 4}


def test_removed_projects(db):
    s = make_server(db)
    ps = ProjectStore(db)
    ps.replace_all(s.server_id, [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}])
    with db.tx() as c:
        c.execute("INSERT INTO issues(server_id, issue_id, project_id) VALUES(?, 1, 2)", (s.server_id,))
    ps.replace_all(s.server_id, [])
    assert ps.get(s.server_id, 1) is None
    assert ps.get(s.server_id, 2).accessible is False


def test_settings_inheritance(db):
    s = make_server(db)
    ps = ProjectStore(db)
    ps.replace_all(s.server_id, [
        {"id": 1, "name": "root"},
        {"id": 2, "name": "child", "parent": {"id": 1, "name": "root"}},
        {"id": 3, "name": "grand", "parent": {"id": 2, "name": "child"}},
    ])
    sid = s.server_id
    assert ps.effective_setting(sid, 3, "watch_versions") is False
    assert ps.effective_setting(sid, 3, "notify") is None
    ps.set_setting(sid, 1, "watch_versions", True)
    assert ps.effective_setting(sid, 3, "watch_versions") is True
    ps.set_setting(sid, 2, "watch_versions", False)
    assert ps.effective_setting(sid, 3, "watch_versions") is False
    assert ps.projects_with_setting(sid, "watch_versions") == {1}
    ps.set_setting(sid, 2, "watch_versions", None)
    assert ps.projects_with_setting(sid, "watch_versions") == {1, 2, 3}


def test_projects_showing_a_relation_follow_inheritance_and_server_default(db):
    """「自分が作成」を表示するプロジェクト(作成したチケットを取得する)は、継承とサーバーの既定で決まる。既定では、どこも表示しない。"""
    s = make_server(db)
    ps = ProjectStore(db)
    ps.replace_all(s.server_id, [
        {"id": 1, "name": "root"},
        {"id": 2, "name": "child", "parent": {"id": 1, "name": "root"}},
        {"id": 3, "name": "other"},
    ])
    sid = s.server_id
    assert ps.projects_showing(sid, "authored", None) == set()
    ps.set_visible_relations(sid, 1, ["assigned", "authored"])  # 親で設定すると、子にも効く
    assert ps.projects_showing(sid, "authored", None) == {1, 2}
    ps.set_visible_relations(sid, 2, ["assigned"])  # 子で外すと、子だけ外れる
    assert ps.projects_showing(sid, "authored", None) == {1}
    # サーバーの既定に含めると、独自に設定していないプロジェクトに効く
    assert ps.projects_showing(sid, "authored", ["assigned", "authored"]) == {1, 3}


def test_masters(db):
    s = make_server(db)
    ms = MasterStore(db)
    ms.replace(s.server_id, "status", [{"id": 1, "name": "新規", "is_closed": False},
                                       {"id": 5, "name": "終了", "is_closed": True}], ("is_closed",))
    assert ms.names(s.server_id, "status") == {1: "新規", 5: "終了"}
    assert ms.closed_status_ids(s.server_id) == {5}
    ms.remember(s.server_id, "user", 7, "田中")
    ms.remember(s.server_id, "user", 7, "田中 一郎")
    assert ms.names(s.server_id, "user") == {7: "田中 一郎"}
