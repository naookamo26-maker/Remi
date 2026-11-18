from remi.context import AppContext
from remi.store.servers import Server
from remi.system.notifier import Notifier
from remi.system.tray import Tray, make_icon


class FakeBackend:
    def __init__(self):
        self.shown = []

    def show(self, n, on_click):
        self.shown.append(n)
        self.on_click = on_click


class FakeApi:
    open_issue = None
    active = False


def comment(iid, notes="こんにちは", project=1, user="田中"):
    return {"issue_id": iid, "journal_id": iid * 10, "user_name": user, "notes": notes,
            "subject": f"件名{iid}", "project_id": project}


def setup(db):
    ctx = AppContext(db)
    ctx.settings.set("language", "ja")
    sid = ctx.servers.save(Server(name="S", url="http://s"), api_key="k").server_id
    ctx.projects.replace_all(sid, [{"id": 1, "name": "A"}, {"id": 2, "name": "B", "parent": {"id": 1, "name": "A"}}])
    opened = []
    backend = FakeBackend()
    api = FakeApi()
    n = Notifier(ctx, api, lambda s, i: opened.append((s, i)), backend=backend)
    return ctx, sid, n, backend, api, opened


def test_aggregates_and_opens_issue(db):
    ctx, sid, n, backend, api, opened = setup(db)
    ctx.bus.publish("newComments", serverId=sid, comments=[comment(5, "古い"), comment(6, "x" * 300)])
    [shown] = backend.shown
    assert shown.title == "#6 件名6"
    assert shown.body.startswith("田中: xxx") and shown.body.count("x") == 120 + 0
    assert "ほか 1件の新着コメント" in shown.body
    backend.on_click()
    assert opened == [(sid, 6)]


def test_respects_settings(db):
    ctx, sid, n, backend, api, opened = setup(db)
    ctx.settings.set("notify_enabled", False)
    ctx.bus.publish("newComments", serverId=sid, comments=[comment(5)])
    assert backend.shown == []
    ctx.settings.set("notify_enabled", True)

    ctx.projects.set_setting(sid, 1, "notify", False)  # 親でオフ → 子もオフ
    ctx.bus.publish("newComments", serverId=sid, comments=[comment(5, project=2)])
    assert backend.shown == []
    ctx.projects.set_setting(sid, 2, "notify", True)
    ctx.bus.publish("newComments", serverId=sid, comments=[comment(5, project=2)])
    assert len(backend.shown) == 1

    ctx.servers.update_fields(sid, notify_enabled=False)
    ctx.projects.set_setting(sid, 1, "notify", None)
    ctx.bus.publish("newComments", serverId=sid, comments=[comment(7, project=1)])
    assert len(backend.shown) == 1


def test_not_for_open_issue_while_active(db):
    ctx, sid, n, backend, api, opened = setup(db)
    api.open_issue = (sid, 5)
    api.active = True
    ctx.bus.publish("newComments", serverId=sid, comments=[comment(5)])
    assert backend.shown == []
    api.active = False  # ウィンドウが非アクティブなら通知する
    ctx.bus.publish("newComments", serverId=sid, comments=[comment(5)])
    assert len(backend.shown) == 1


def test_snooze(db):
    ctx, sid, n, backend, api, opened = setup(db)
    n.snooze(60)
    ctx.bus.publish("newComments", serverId=sid, comments=[comment(5)])
    assert backend.shown == []
    n.resume()
    ctx.bus.publish("newComments", serverId=sid, comments=[comment(5)])
    assert len(backend.shown) == 1


def test_follows_visible_relations_and_filter(db):
    from remi.store.issues import REL_ASSIGNED, REL_WATCHING

    ctx, sid, n, backend, api, opened = setup(db)
    ctx.issues.upsert_issue(sid, {"id": 5, "project": {"id": 1}, "subject": "担当"}, REL_ASSIGNED)
    ctx.issues.upsert_issue(sid, {"id": 6, "project": {"id": 1}, "subject": "ウォッチ"}, REL_WATCHING)
    # 一覧に出すチケット(プロジェクトごとの表示するチケット・絞り込み)だけを通知する。子プロジェクトにも効く
    ctx.projects.set_visible_relations(sid, 1, ["assigned", "group", "authored"])
    ctx.bus.publish("newComments", serverId=sid, comments=[comment(5), comment(6)])
    [shown] = backend.shown
    assert shown.issue_id == 5 and "ほか" not in shown.body
    ctx.projects.set_visible_relations(sid, 1, None)
    ctx.settings.set("relation_filter", "watching")
    ctx.bus.publish("newComments", serverId=sid, comments=[comment(5)])
    assert len(backend.shown) == 1
    ctx.bus.publish("newComments", serverId=sid, comments=[comment(6)])
    assert backend.shown[-1].issue_id == 6


def test_tray_icon_and_menu(db):
    ctx, sid, n, backend, api, opened = setup(db)
    calls = []
    tray = Tray(ctx, on_open=lambda: calls.append("open"), on_settings=lambda: calls.append("settings"),
                on_sync=lambda: calls.append("sync"), on_quit=lambda: calls.append("quit"), notifier=n)
    for unread in (False, True):
        for dark in (False, True):
            img = make_icon(unread, dark)
            assert img.size == (64, 64)
    assert make_icon(True, False).getpixel((52, 12))[0] > 150  # 未読の赤い点
    items = tray.menu_items()
    labels = [label for label, _ in items]
    assert labels[0] == "Remiを開く" and "通知を1時間停止" in labels and labels[-1] == "終了"
    assert tray.unread_total() == 0
    dict(items)["通知を1時間停止"]()
    assert n.snoozed
    assert "通知を再開" in [label for label, _ in tray.menu_items()]
    dict(tray.menu_items())["終了"]()
    assert calls == ["quit"]


def test_does_not_notify_unrelated_version_issues(db):
    """バージョンで見ているだけの(自分と関係ない)チケットのコメントは通知しない。"""
    ctx, sid, n, backend, api, opened = setup(db)
    ctx.issues.upsert_issue_data(sid, {"id": 7, "project": {"id": 1}, "subject": "関係ない"})
    ctx.bus.publish("newComments", serverId=sid, comments=[comment(7)])
    assert backend.shown == []
