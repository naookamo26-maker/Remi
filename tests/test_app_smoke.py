from remi.context import AppContext
from remi.store.servers import Server
from remi.ui.main_window import MainWindow
from remi.ui.tree import NodeKey, visible_projects
from remi.store.projects import Project


def test_main_window_hides_to_tray_when_available(qapp, db):
    w = MainWindow(AppContext(db))
    w.minimize_to_tray = True
    w.show()
    w.close()
    assert not w.isVisible()


def test_tree_shows_servers_projects_and_unread(qapp, db):
    ctx = AppContext(db)
    s = ctx.servers.save(Server(name="社内", url="http://x", my_user_id=42), api_key="k")
    sid = s.server_id
    ctx.projects.replace_all(sid, [
        {"id": 1, "name": "基幹"},
        {"id": 2, "name": "受注", "parent": {"id": 1, "name": "基幹"}},
        {"id": 3, "name": "空", "parent": {"id": 1, "name": "基幹"}},
    ])
    ctx.issues.upsert_issue(sid, {"id": 10, "project": {"id": 2}, "subject": "x",
                                  "updated_on": "2026-01-01T00:00:00Z"}, 1)
    ctx.issues.upsert_journals(sid, 10, [{"id": 1, "user": {"id": 7, "name": "田中"}, "notes": "a"},
                                         {"id": 2, "user": {"id": 42, "name": "me"}, "notes": "b"}])
    w = MainWindow(ctx)
    tree = w.tree
    items = tree._items
    assert items[NodeKey("server_unread", sid).token()].data(tree_roles()[0]) == 1
    assert items[NodeKey("project", sid, 1).token()].data(tree_roles()[0]) == 1  # 子の合計
    assert NodeKey("project", sid, 3).token() not in items  # 監視対象のないプロジェクトは隠す
    sel = tree.selection_for(NodeKey("project", sid, 1), "基幹")
    assert sel.project_ids == {1, 2, 3}
    assert sel.breadcrumb == "基幹"


def tree_roles():
    from remi.ui.tree import UnreadRole

    return (UnreadRole,)


def test_visible_projects_includes_ancestors():
    ps = [Project(1, 1, None, "a", None, 1, True), Project(1, 2, 1, "b", None, 1, True),
          Project(1, 3, None, "c", None, 1, True), Project(1, 4, None, "archived", None, 9, True)]
    assert visible_projects(ps, {2}, False) == {1, 2}
    assert visible_projects(ps, {2}, True) == {1, 2, 3}


def test_server_dialog_requires_successful_test(qapp, fake):
    import time

    from PySide6.QtCore import QCoreApplication

    from fake_redmine import API_KEY, ME
    from remi.ui.server_dialog import ServerDialog

    fake.add_project(1, "A")
    fake.add_issue(1, 1, "x", description="h2. 見出し\n\n<pre>code</pre>")
    dlg = ServerDialog()
    dlg.url_edit.setText(fake.base_url)
    dlg.key_edit.setText(API_KEY)
    assert not dlg.save_button.isEnabled()
    dlg.run_test()
    end = time.monotonic() + 10
    while dlg.result_probe is None and time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    assert dlg.result_probe is not None and dlg.result_probe.user_id == ME
    assert dlg.save_button.isEnabled()
    assert dlg.text_format() == "textile"  # 推測結果が初期値になる
    server, key = dlg.build_server()
    assert server.my_user_id == ME and server.my_group_ids == [100] and key == API_KEY
    # URLを変えたら、もう一度テストが必要
    dlg.url_edit.setText(fake.base_url + "/other")
    assert not dlg.save_button.isEnabled()


def test_server_dialog_shows_error(qapp, fake):
    import time

    from PySide6.QtCore import QCoreApplication

    from remi.ui.server_dialog import ServerDialog

    dlg = ServerDialog()
    dlg.url_edit.setText(fake.base_url)
    dlg.key_edit.setText("wrong")
    dlg.run_test()
    end = time.monotonic() + 10
    while not dlg.test_label.property("error") and time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    assert dlg.test_label.property("error")
    assert not dlg.save_button.isEnabled()
