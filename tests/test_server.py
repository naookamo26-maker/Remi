import json
import threading
import time

import pytest
import requests

from fake_redmine import API_KEY, ME

from remi.context import AppContext
from remi.server.api import RemiApi
from remi.server.http import LocalServer
from remi.sync.worker import SyncManager


def wait_until(cond, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def app(db, fake):
    ctx = AppContext(db)
    sync = SyncManager(db, ctx.bus)
    api = RemiApi(ctx, sync)
    server = LocalServer(api)
    server.start()
    s = requests.Session()
    s.trust_env = False
    r = s.get(server.start_url)
    assert r.status_code == 200 and "<title>Remi</title>" in r.text

    def rpc(method, **params):
        res = s.post(f"{server.origin}/api/{method}", json=params)
        assert res.status_code == 200, res.text
        body = res.json()
        if "error" in body:
            raise AssertionError(body["error"])
        return body["result"]

    def rpc_error(method, **params):
        return s.post(f"{server.origin}/api/{method}", json=params).json()["error"]

    yield type("App", (), {"ctx": ctx, "api": api, "server": server, "http": s, "rpc": staticmethod(rpc),
                           "rpc_error": staticmethod(rpc_error), "sync": sync})()
    sync.stop_all()
    server.stop()


def test_requires_token_cookie_and_host(app):
    origin = app.server.origin
    plain = requests.Session()
    plain.trust_env = False
    assert plain.get(origin + "/").status_code == 403
    assert plain.get(origin + "/?token=wrong").status_code == 403
    assert plain.post(origin + "/api/bootstrap", json={}).status_code == 403
    # Cookie があっても Host が違えば拒否する(DNSリバインディング対策)
    r = app.http.get(origin + "/", headers={"Host": "evil.example:80"})
    assert r.status_code == 403
    assert app.http.post(origin + "/api/bootstrap", data="{}", headers={"Content-Type": "text/plain"}).status_code == 415


def test_security_headers_and_static(app):
    r = app.http.get(app.server.origin + "/js/api.js")
    assert r.status_code == 200 and r.headers["Content-Type"].startswith("text/javascript")
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert app.http.get(app.server.origin + "/../../pyproject.toml").status_code == 404
    assert app.http.get(app.server.origin + "/%2e%2e/app.py").status_code == 404
    assert app.http.get(app.server.origin + "/highlight.css").text.count(".theme-dark .hl") > 3


def test_unknown_method_and_bad_params(app):
    assert app.http.post(app.server.origin + "/api/_servers_changed", json={}).status_code == 404
    assert app.http.post(app.server.origin + "/api/getConversation", json={"x": 1}).status_code == 400


def test_bootstrap_and_settings(app):
    boot = app.rpc("bootstrap")
    assert boot["lang"] in ("ja", "en") and boot["catalog"] and boot["hasServers"] is False
    s = app.rpc("setSetting", key="theme", value="dark")
    assert s["theme"] == "dark"
    assert app.rpc_error("setSetting", key="theme", value="pink")["code"] == "invalid_setting"
    assert app.rpc_error("setSetting", key="window_geometry", value="x")["code"] == "invalid_setting"


def test_full_flow(app, fake):
    fake.add_project(1, "基幹")
    fake.add_project(2, "受注", parent=1)
    fake.add_issue(10, 2, "CSVが文字化けする", description="```python\nprint(1)\n```")
    fake.add_journal(10, 7, "再現手順です ![画面](shot.png)")
    fake.add_attachment(10, 77, "shot.png", b"\x89PNG-data")

    assert app.rpc_error("testConnection", url=fake.base_url, apiKey="wrong")["code"] == "unauthorized"
    probe = app.rpc("testConnection", url=fake.base_url, apiKey=API_KEY)
    assert probe["userId"] == ME and probe["formatGuess"]["textFormat"] == "common_mark"
    assert app.rpc_error("saveServer", data={"name": "S", "url": fake.base_url, "apiKey": API_KEY})["code"] == "not_tested"
    saved = app.rpc("saveServer", data={
        "name": "社内", "url": fake.base_url, "apiKey": API_KEY, "textFormat": "common_mark",
        "pollInterval": 180, "userId": probe["userId"], "login": probe["login"], "userName": probe["name"],
        "groupIds": probe["groupIds"]})
    sid = saved["serverId"]
    assert "apiKey" not in json.dumps(app.rpc("listServers"))

    events = []
    stop = threading.Event()

    def listen():
        with app.http.get(app.server.origin + "/events", stream=True, timeout=30) as r:
            for line in r.iter_lines(chunk_size=1):
                if stop.is_set():
                    break
                if line.startswith(b"data: "):
                    events.append(json.loads(line[6:]))

    threading.Thread(target=listen, daemon=True).start()

    assert wait_until(lambda: app.ctx.issues.get(sid, 10) is not None and app.ctx.issues.journals_synced(sid, 10))
    tree = app.rpc("getTree")
    server = tree["servers"][0]
    assert server["name"] == "社内" and server["projects"][0]["name"] == "基幹"
    assert server["projects"][0]["children"][0]["count"] == 1

    issues = app.rpc("listIssues", serverId=sid, projectId=1)["issues"]
    assert [i["issueId"] for i in issues] == [10]
    assert issues[0]["relations"] == ["assigned"] and issues[0]["unread"] == 0

    conv = app.rpc("getConversation", serverId=sid, issueId=10)
    assert conv["subject"] == "CSVが文字化けする" and conv["breadcrumb"] == ["基幹", "受注"]
    assert 'class="code hl"' in conv["descriptionHtml"]
    assert '<img src="/attachments/' in conv["journals"][0]["html"]

    img = app.http.get(app.server.origin + f"/attachments/{sid}/77")
    assert img.status_code == 200 and img.content == b"\x89PNG-data" and img.headers["Content-Type"] == "image/png"

    # 新着コメント → 未読 → 開くと既読
    fake.add_journal(10, 7, "追加の情報です")
    app.rpc("syncNow")
    assert wait_until(lambda: app.ctx.issues.unread_count(sid, 10) == 1)
    assert wait_until(lambda: any(e["type"] == "newComments" for e in events))
    assert app.rpc("listIssues", unreadOnly=True)["issues"][0]["unread"] == 1
    app.rpc("getConversation", serverId=sid, issueId=10)
    assert app.ctx.issues.unread_count(sid, 10) == 0

    # 投稿
    app.rpc("postNote", serverId=sid, issueId=10, body="対応します", private=True)
    assert wait_until(lambda: any(e["type"] == "noteFinished" for e in events))
    assert wait_until(lambda: any(j.notes == "対応します" and j.private_notes for j in app.ctx.issues.journals(sid, 10)))
    conv = app.rpc("getConversation", serverId=sid, issueId=10)
    assert conv["journals"][-1]["mine"] and conv["journals"][-1]["private"]

    # プロジェクト単位の設定
    ps = app.rpc("setProjectSetting", serverId=sid, projectId=1, key="notify", value=False)
    assert ps["notify"] == {"value": False, "effective": False}
    assert app.rpc("getProjectSettings", serverId=sid, projectId=2)["notify"]["effective"] is False

    stop.set()
    app.rpc("deleteServer", serverId=sid)
    assert app.rpc("listServers") == []


def test_preview_and_external_open_validation(app, fake):
    app.ctx.servers.save(__import__("remi.store.servers", fromlist=["Server"]).Server(name="S", url=fake.base_url), api_key=API_KEY)
    assert "<strong>x</strong>" in app.rpc("preview", serverId=1, issueId=1, text="**x**")
    assert app.rpc_error("openExternal", url="file:///etc/passwd")["code"] == "invalid_url"


def test_autostart_setting_calls_registry_hook(db):
    from remi.context import AppContext
    from remi.server.api import RemiApi

    calls = []
    api = RemiApi(AppContext(db), autostart=calls.append)
    api.setSetting("autostart", True)
    api.setSetting("autostart", False)
    assert calls == [True, False]


def test_open_issue_and_active_are_tracked(db):
    from remi.context import AppContext
    from remi.server.api import RemiApi

    api = RemiApi(AppContext(db))
    api.setOpenIssue(1, 5)
    api.setActive(False)
    assert api.open_issue == (1, 5) and api.active is False
    api.setOpenIssue(None, None)
    assert api.open_issue is None


def _register(app, fake):
    from remi.store.servers import Server

    sid = app.ctx.servers.save(Server(name="S", url=fake.base_url, my_user_id=ME, my_group_ids=[100]),
                               api_key=API_KEY).server_id
    app.sync.restart()
    return sid


def test_relation_filter(app, fake):
    fake.add_project(1, "A")
    fake.add_issue(10, 1, "担当")
    fake.add_issue(11, 1, "ウォッチ", assigned_to=7, watchers={ME})
    fake.add_issue(12, 1, "両方", watchers={ME})
    fake.add_issue(13, 1, "グループ", assigned_to=100)
    sid = _register(app, fake)
    assert wait_until(lambda: len(app.ctx.issues.list_issues([sid])) == 4)
    ids = lambda rel: sorted(i["issueId"] for i in app.rpc("listIssues", relation=rel)["issues"])  # noqa: E731
    assert ids("all") == [10, 11, 12, 13]
    assert ids("assigned") == [10, 12, 13]
    assert ids("watching") == [11, 12]
    # どのプロジェクトでも「自分が作成」を表示していないので、「作成」での絞り込みは、「すべて」として扱う(画面ではボタンが隠れる)
    assert ids("authored") == [10, 11, 12, 13]
    assert app.rpc_error("listIssues", relation="x")["code"] == "invalid_input"
    assert app.rpc("setSetting", key="relation_filter", value="watching")["relation_filter"] == "watching"


def test_visible_relations_filter_list_and_tree(app, fake):
    fake.add_project(1, "A")
    fake.add_project(2, "A-子", parent=1)
    fake.add_project(3, "B")
    fake.add_issue(10, 1, "担当")
    # 更新日時は秒単位なので、同じ秒にコメントを足すと更新に気づけない。あらかじめ過去にしておく
    fake.add_issue(11, 1, "ウォッチ", assigned_to=7, watchers={ME}, updated_on="2026-01-01T00:00:00Z")
    fake.add_issue(13, 1, "グループ", assigned_to=100)
    fake.add_issue(20, 2, "子のウォッチ", assigned_to=7, watchers={ME})
    fake.add_issue(30, 3, "Bのウォッチ", assigned_to=7, watchers={ME})
    sid = _register(app, fake)
    assert wait_until(lambda: len(app.ctx.issues.list_issues([sid])) == 5)
    fake.add_journal(11, 7, "未読のコメント")
    app.rpc("syncNow")
    assert wait_until(lambda: app.rpc("getTree")["unread"] == 1)
    ids = lambda **kw: sorted(i["issueId"] for i in app.rpc("listIssues", **kw)["issues"])  # noqa: E731
    assert ids() == [10, 11, 13, 20, 30]
    projects = lambda: {p["projectId"]: p for p in app.rpc("getTree")["servers"][0]["projects"]}  # noqa: E731
    assert projects()[1]["relations"] == ["assigned", "group", "watching"]  # 既定は、「自分が作成」を含まない
    assert projects()[1]["relationsOwn"] is False

    # A でウォッチ中を表示しない: A と子プロジェクトのウォッチだけのチケットが、一覧からも、ツリーの件数・
    # 未読数からも外れる(プロジェクトは残す)。B には効かない
    ps = app.rpc("setProjectSetting", serverId=sid, projectId=1, key="visible_relations",
                 value=["authored", "assigned", "group"])
    assert ps["visible_relations"] == {"value": ["assigned", "group", "authored"],
                                       "effective": ["assigned", "group", "authored"]}
    child = app.rpc("getProjectSettings", serverId=sid, projectId=2)["visible_relations"]
    assert child == {"value": None, "effective": ["assigned", "group", "authored"]}
    assert ids() == [10, 13, 30]
    assert ids(serverId=sid, projectId=1) == [10, 13]
    tree = app.rpc("getTree")
    [server] = tree["servers"]
    assert server["count"] == 3 and server["unread"] == 0 and tree["unread"] == 0
    a = projects()[1]
    assert a["count"] == 2 and a["relationsOwn"] is True and a["relations"] == ["assigned", "group", "authored"]
    # 子プロジェクトは、表示するチケットがなくなる(ウォッチだけだった)ので、ツリーから消える
    assert a["children"] == []
    assert projects()[3]["count"] == 1
    # 子プロジェクトで上書きできる
    app.rpc("setProjectSetting", serverId=sid, projectId=2, key="visible_relations", value=["watching"])
    assert ids() == [10, 13, 20, 30]

    # 絞り込みボタンで絞ると、ツリーの件数・未読数とトレイの未読数も一覧と一致する
    from remi.system.tray import Tray

    tray = Tray(app.ctx, on_open=lambda: None, on_settings=lambda: None, on_sync=lambda: None, on_quit=lambda: None)
    app.rpc("setProjectSetting", serverId=sid, projectId=1, key="visible_relations", value=None)
    app.rpc("setProjectSetting", serverId=sid, projectId=2, key="visible_relations", value=None)
    # 一覧の絞り込みボタンは、真ん中の一覧だけに効く。ツリーの件数・未読数とトレイの未読数は変わらない
    assert ids(relation="watching") == [11, 20, 30]
    for value in ("watching", "assigned", "all"):
        app.rpc("setSetting", key="relation_filter", value=value)
        server = app.rpc("getTree")["servers"][0]
        assert server["count"] == 5 and server["unread"] == 1 and tray.unread_total() == 1, value
    app.rpc("setSetting", key="relation_filter", value="assigned")
    assert ids() == [10, 11, 13, 20, 30]  # relation を渡さない一覧は、絞らない
    # 絞り込みは、各プロジェクトの表示するチケットの中で効く(表示しない関係のチケットは出さない)
    app.rpc("setProjectSetting", serverId=sid, projectId=3, key="visible_relations", value=["assigned"])
    app.rpc("setSetting", key="relation_filter", value="watching")
    assert ids(relation="watching") == [11, 20]
    # どのプロジェクトでも表示しない関係で絞り込んでいたら「すべて」として扱う(画面ではボタンが隠れる)
    for pid in (1, 3):
        app.rpc("setProjectSetting", serverId=sid, projectId=pid, key="visible_relations", value=["assigned"])
    assert app.rpc("getTree")["relationsInUse"] == ["assigned"]
    assert app.rpc("getTree")["servers"][0]["count"] == 1 and tray.unread_total() == 0
    app.rpc("setSetting", key="relation_filter", value="all")
    # 自分の担当だけ: グループ経由の担当は外れる
    assert ids() == [10]
    # 継承に戻す
    app.rpc("setProjectSetting", serverId=sid, projectId=3, key="visible_relations", value=None)
    assert ids() == [10, 30]
    # 知らない関係・重複・リストでない値は受け付けない
    for bad in (["x"], ["assigned", "assigned"], "assigned", True):
        err = app.rpc_error("setProjectSetting", serverId=sid, projectId=1, key="visible_relations", value=bad)
        assert err["code"] == "invalid_setting"
    # グローバル設定としては、もう受け付けない
    for key in ("visible_relations", "show_all_projects"):
        assert app.rpc_error("setSetting", key=key, value=True)["code"] == "invalid_setting"


def test_authored_issues_are_fetched_when_the_filter_shows_them(app, fake):
    """「表示するチケット」の「自分が作成」にチェックすると、作成したチケットも取得する(通信が増えるので、既定は含めない)。"""
    fake.add_project(1, "A")
    fake.add_issue(10, 1, "担当")
    fake.add_issue(20, 1, "自分が作成", assigned_to=7, author=ME)
    sid = _register(app, fake)
    assert wait_until(lambda: app.ctx.issues.get(sid, 10) is not None)
    time.sleep(1.5)
    assert app.ctx.issues.get(sid, 20) is None  # 既定では、作成したチケットは取得しない
    app.rpc("setProjectSetting", serverId=sid, projectId=1, key="visible_relations",
            value=["assigned", "group", "watching", "authored"])
    assert wait_until(lambda: app.ctx.issues.get(sid, 20) is not None, timeout=20)
    assert [i["issueId"] for i in app.rpc("listIssues", relation="authored")["issues"]] == [20]


def test_show_all_projects_per_server(app, fake):
    from remi.store.servers import Server

    fake.add_project(1, "チケットあり")
    fake.add_project(2, "チケットなし")
    fake.add_issue(10, 1, "担当")
    sid = _register(app, fake)
    other = app.ctx.servers.save(Server(name="T", url=fake.base_url, my_user_id=ME), api_key=API_KEY).server_id
    app.sync.restart()
    names = lambda i: [p["name"] for p in app.rpc("getTree")["servers"][i]["projects"]]  # noqa: E731
    assert wait_until(lambda: names(0) == ["チケットあり"] and names(1) == ["チケットあり"])
    assert app.rpc("getTree")["servers"][0]["showAllProjects"] is False
    # 隠れているプロジェクトの数(「すべてのプロジェクトを表示」のリンクに出す)
    assert app.rpc("getTree")["servers"][0]["hiddenProjects"] == 1
    app.rpc("setServerSetting", serverId=sid, key="show_all_projects", value=True)
    tree = app.rpc("getTree")
    assert tree["servers"][0]["showAllProjects"] is True
    assert tree["servers"][0]["hiddenProjects"] == 1  # 表示していても、「隠す」リンクのために数える
    assert names(0) == ["チケットあり", "チケットなし"]
    # ふだんは隠れていて、「すべてを表示」で出ているプロジェクトには印が付く(画面では薄い色にする)
    flags = {p["name"]: p["shownOnlyByShowAll"] for p in app.rpc("getTree")["servers"][0]["projects"]}
    assert flags == {"チケットあり": False, "チケットなし": True}
    assert names(1) == ["チケットあり"]  # ほかのサーバーには効かない
    assert app.ctx.servers.get(other).show_all_projects is False
    assert app.rpc_error("setServerSetting", serverId=999, key="show_all_projects", value=True)["code"] == "not_found"
    for key, bad in (("show_all_projects", "yes"), ("visible_relations", ["x"]), ("x", True)):
        assert app.rpc_error("setServerSetting", serverId=sid, key=key, value=bad)["code"] == "invalid_setting"


def test_empty_relations_hide_project_from_tree(app, fake):
    """表示するチケットをすべて外せる。表示するものがないプロジェクトは、ツリーから消える。"""
    fake.add_project(1, "A")
    fake.add_project(2, "B")
    fake.add_issue(10, 1, "担当")
    fake.add_issue(20, 2, "担当")
    sid = _register(app, fake)
    assert wait_until(lambda: len(app.ctx.issues.list_issues([sid])) == 2)
    names = lambda: [p["name"] for p in app.rpc("getTree")["servers"][0]["projects"]]  # noqa: E731
    assert names() == ["A", "B"]
    ps = app.rpc("setProjectSetting", serverId=sid, projectId=1, key="visible_relations", value=[])
    assert ps["visible_relations"] == {"value": [], "effective": []}
    assert names() == ["B"]
    assert [i["issueId"] for i in app.rpc("listIssues")["issues"]] == [20]
    # 一覧の絞り込みボタンでは、ツリーの形は変わらない
    app.rpc("setSetting", key="relation_filter", value="watching")
    assert names() == ["B"]
    app.rpc("setSetting", key="relation_filter", value="all")
    # 「すべてのプロジェクトを表示」にすれば、表示するものがなくても出る(ここから戻せる)
    app.rpc("setServerSetting", serverId=sid, key="show_all_projects", value=True)
    assert names() == ["A", "B"]
    app.rpc("setServerSetting", serverId=sid, key="show_all_projects", value=False)
    assert app.rpc("getTree")["servers"][0]["hiddenProjects"] == 1
    # サーバーの既定もすべて外せる。独自の設定があるプロジェクトだけが残る
    app.rpc("setServerSetting", serverId=sid, key="visible_relations", value=[])
    assert names() == []
    assert app.rpc("getTree")["servers"][0]["hiddenProjects"] == 2
    app.rpc("setProjectSetting", serverId=sid, projectId=1, key="visible_relations", value=["assigned"])
    assert names() == ["A"]
    tree = app.rpc("getTree")["servers"][0]
    assert tree["relations"] == [] and tree["relationsOwn"] is True


def test_server_default_visible_relations(app, fake):
    """サーバーの既定は、独自に設定していないプロジェクト(子を含む)に効く。"""
    fake.add_project(1, "A")
    fake.add_project(2, "A-子", parent=1)
    fake.add_project(3, "B")
    fake.add_issue(10, 1, "担当")
    fake.add_issue(20, 2, "子のウォッチ", assigned_to=7, watchers={ME})
    fake.add_issue(30, 3, "Bのウォッチ", assigned_to=7, watchers={ME})
    sid = _register(app, fake)
    assert wait_until(lambda: len(app.ctx.issues.list_issues([sid])) == 3)
    ids = lambda: sorted(i["issueId"] for i in app.rpc("listIssues")["issues"])  # noqa: E731
    app.rpc("setServerSetting", serverId=sid, key="visible_relations", value=["assigned", "group"])
    assert ids() == [10]
    [server] = app.rpc("getTree")["servers"]
    assert server["relations"] == ["assigned", "group"] and server["relationsOwn"] is True
    assert server["count"] == 1
    # 表示するチケットがない B と A-子 は、ツリーから消える
    assert [(p["name"], p["relations"], p["children"]) for p in server["projects"]] == [("A", ["assigned", "group"], [])]
    assert app.rpc("getProjectSettings", serverId=sid, projectId=2)["visible_relations"] == {
        "value": None, "effective": ["assigned", "group"]}
    # プロジェクトの独自設定はサーバーの既定より優先し、子プロジェクトにも効く
    app.rpc("setProjectSetting", serverId=sid, projectId=1, key="visible_relations", value=["watching"])
    assert ids() == [20]
    [a] = app.rpc("getTree")["servers"][0]["projects"]
    assert [c["name"] for c in a["children"]] == ["A-子"]
    app.rpc("setServerSetting", serverId=sid, key="visible_relations", value=None)
    assert ids() == [20, 30]
    assert app.rpc("getTree")["servers"][0]["relationsOwn"] is False


def test_write_permissions(app, fake):
    fake.add_project(1, "A")
    fake.add_project(2, "終了したプロジェクト", status=5)
    fake.add_project(3, "メンバーでない")
    fake.members[1] = [ME, 7]
    fake.add_issue(10, 1, "x")
    fake.add_issue(20, 2, "y")
    fake.add_issue(30, 3, "z", assigned_to=7, watchers={ME})
    fake.my_roles = {1: [3]}
    fake.roles = {3: ["view_issues", "add_issue_notes"]}
    sid = _register(app, fake)
    assert wait_until(lambda: all(app.ctx.issues.get(sid, i) for i in (10, 20, 30)))
    perms = lambda iid: app.rpc("getEditOptions", serverId=sid, issueId=iid)["permissions"]  # noqa: E731

    # コメントだけできるロール
    assert perms(10) == {"editFields": False, "changeStatus": False, "comment": True, "privateNotes": False,
                         "attach": True, "editNotes": "none", "reason": None, "assignee": True,
                         "subtasks": False, "watchAdd": False, "watchRemove": False}
    # 終了したプロジェクトには書き込めない
    assert perms(20) == {"editFields": False, "changeStatus": False, "comment": False, "privateNotes": False,
                         "attach": False, "editNotes": "none", "reason": "project_closed", "assignee": False,
                         "subtasks": False, "watchAdd": False, "watchRemove": False}
    # メンバーでない(権限が分からない)ときは制限しない
    assert perms(30)["editFields"] and perms(30)["comment"]
    # ただし他の人のコメントまで編集できるとはしない(自分のコメントだけ「⋯」から操作できる)
    assert perms(30)["editNotes"] == "own"

    # 権限はしばらく覚えておくので、変えたら覚えた分を捨てる
    fake.roles = {3: ["view_issues"], 4: ["edit_issues", "add_issue_notes", "set_notes_private"]}
    fake.my_roles = {1: [3]}
    app.api._role_perms.clear()
    assert perms(10) == {"editFields": False, "changeStatus": False, "comment": False, "privateNotes": False,
                         "attach": False, "editNotes": "none", "reason": "no_permission", "assignee": True,
                         "subtasks": False, "watchAdd": False, "watchRemove": False}
    fake.my_roles = {1: [3, 4]}
    app.api._my_roles.clear()
    assert perms(10)["reason"] is None
    assert perms(10)["editFields"] and perms(10)["changeStatus"] and perms(10)["privateNotes"]
    # ワークフロー上の遷移先がなければ、ステータスだけ変えられない
    fake.allowed_status_ids = []
    assert not perms(10)["changeStatus"] and perms(10)["editFields"]
    fake.allowed_status_ids = None
    # 管理者は何でもできる
    fake.my_roles = {}
    fake.admin = True
    app.api._my_roles.clear()
    assert perms(10)["editFields"] and perms(30)["comment"]
    assert perms(10)["editNotes"] == "all"


def test_edit_upload_and_watch(app, fake):
    fake.add_project(1, "A")
    fake.members[1] = [ME, 7]
    fake.add_issue(10, 1, "x")
    sid = _register(app, fake)
    assert wait_until(lambda: app.ctx.issues.get(sid, 10) is not None and app.ctx.issues.journals_synced(sid, 10))

    opts = app.rpc("getEditOptions", serverId=sid, issueId=10)
    assert [s["name"] for s in opts["statuses"]] == ["新規", "進行中", "終了"] and opts["statusesExact"]
    assert [s["closed"] for s in opts["statuses"]] == [False, False, True]
    assert {a["id"] for a in opts["assignees"]} == {ME, 7}
    assert opts["current"]["assigned_to_id"] == ME and opts["me"] == ME

    # アップロード(専用ヘッダーがないものは拒否)
    url = f"{app.server.origin}/upload?serverId={sid}&filename=clip.png"
    assert app.http.post(url, data=b"\x89PNG").status_code == 403
    up = app.http.post(url, data=b"\x89PNG", headers={"X-Remi-Upload": "1"}).json()["result"]
    assert up["isImage"] and up["token"]
    assert app.http.get(app.server.origin + up["url"]).content == b"\x89PNG"
    out = app.rpc("preview", serverId=sid, issueId=10, text="![](clip.png)",
                  pending=[{"filename": "clip.png", "id": up["id"]}])
    assert f'src="{up["url"]}"' in out

    # 変更 + 添付だけ(コメントなし)でも送れる
    assert app.rpc_error("postNote", serverId=sid, issueId=10, changes={"subject": "x"})["code"] == "invalid_input"
    app.rpc("postNote", serverId=sid, issueId=10, body="", changes={"status_id": 2, "assigned_to_id": 7},
            uploads=[{"token": up["token"], "filename": "clip.png", "contentType": "image/png"}])
    assert wait_until(lambda: (app.ctx.issues.get(sid, 10).status_id == 2))
    # 送信できた添付は、プレビュー用のメモリから消す
    assert wait_until(lambda: app.http.get(app.server.origin + up["url"]).status_code == 404)
    conv = app.rpc("getConversation", serverId=sid, issueId=10)
    texts = [d["text"] for j in conv["journals"] for d in j["details"]]
    assert any("clip.png" in t_ for t_ in texts) and conv["assignedToId"] == 7
    # 担当を外れても、ウォッチしていれば監視対象に残る
    assert conv["relations"] == ["assigned"]
    app.rpc("setWatching", serverId=sid, issueId=10, watching=True)
    assert wait_until(lambda: ME in fake.watchers[10] and app.ctx.issues.get(sid, 10).relation_flags & 2)
    app.rpc("setWatching", serverId=sid, issueId=10, watching=False)
    assert wait_until(lambda: ME not in fake.watchers[10])


def test_open_redmine_falls_back_to_browser(db, monkeypatch):
    from remi.context import AppContext
    from remi.server.api import RemiApi

    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)

    class Win:
        def __init__(self):
            self.urls = []

        def open_redmine(self, url):
            self.urls.append(url)
            return True

    win = Win()
    api = RemiApi(AppContext(db), window=win)
    assert api.openRedmine("https://rm.example/issues/1") == "app"
    assert api.openRedmine("https://rm.example/issues/1", external=True) == "browser"
    api.setSetting("redmine_open_in", "browser")
    assert api.openRedmine("https://rm.example/issues/2") == "browser"
    assert win.urls == ["https://rm.example/issues/1"]
    assert opened == ["https://rm.example/issues/1", "https://rm.example/issues/2"]


def test_open_redmine_in_a_tab_and_tab_operations(db, monkeypatch):
    """設定が「タブ」なら、Redmineをタブで開く。タブにできなければ、別ウィンドウ→ブラウザの順に戻る。"""
    from remi.context import AppContext
    from remi.server.api import NullWindow, RemiApi

    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    win = NullWindow(simulate_tabs=True)
    api = RemiApi(AppContext(db), window=win)
    sub = api.ctx.bus.subscribe()
    assert api.bootstrap()["tabsSupported"] is True
    assert api.setSetting("redmine_open_in", "tab")["redmine_open_in"] == "tab"
    assert api.openRedmine("https://rm.example/issues/1") == "tab"
    assert api.openRedmine("https://rm.example/issues/2") == "tab"
    assert api.openRedmine("https://rm.example/issues/3", external=True) == "browser"  # Shift+クリックはブラウザ
    state = api.getTabs()
    assert [x["url"] for x in state["tabs"]] == ["https://rm.example/issues/1", "https://rm.example/issues/2"]
    assert state["active"] == 2
    events = []
    while (e := sub.get(timeout=0.05)) is not None:
        events.append(e)
    assert [e["type"] for e in events if e["type"] == "tabsChanged"]  # タブが変わると、画面に知らせる
    assert events[-1]["active"] == 2 and len(events[-1]["tabs"]) == 2
    api.activateTab(1)
    assert api.getTabs()["active"] == 1
    api.activateTab(None)  # 固定の「Remi」のタブ
    assert api.getTabs()["active"] is None
    api.tabAction(1, "reload")
    for bad in (("x", "back"), (1, "format c:")):
        with pytest.raises(Exception) as err:
            api.tabAction(*bad)
        assert getattr(err.value, "code", "") == "invalid_input"
    with pytest.raises(Exception) as err:
        api.activateTab("1")
    assert err.value.code == "invalid_input"
    api.setTabChromeHeight(76)
    for bad in (0, -1, 1000, "x", True):
        with pytest.raises(Exception) as err:
            api.setTabChromeHeight(bad)
        assert err.value.code == "invalid_input"
    # タブを切り離すと、タブを閉じて、同じページを別のウィンドウで開く
    api.detachTab(1)
    assert win.detached == ["https://rm.example/issues/1"]
    assert [x["id"] for x in api.getTabs()["tabs"]] == [2]
    api.closeTab(2)
    assert api.getTabs() == {"tabs": [], "active": None}
    assert opened == ["https://rm.example/issues/3"]


def test_detached_window_chrome_api(db):
    """切り離したウィンドウの上の操作行が使う API(ページの状態の取得と、ボタンの操作)。"""
    from remi.context import AppContext
    from remi.server.api import NullWindow, RemiApi

    win = NullWindow()
    api = RemiApi(AppContext(db), window=win)
    assert api.getWindowState(1) == {}
    win.window_states[1] = {"url": "https://rm/issues/1", "canBack": True, "canForward": False, "loading": False,
                            "attachable": True}
    assert api.getWindowState(1)["url"] == "https://rm/issues/1"
    for action in ("back", "forward", "reload", "attach"):
        api.windowAction(1, action)
    assert win.window_actions == [(1, "back"), (1, "forward"), (1, "reload"), (1, "attach")]
    for bad in (("1", "back"), (1, "format c:"), (True, "back")):
        with pytest.raises(Exception) as err:
            api.windowAction(*bad)
        assert err.value.code == "invalid_input"
    with pytest.raises(Exception) as err:
        api.getWindowState("x")
    assert err.value.code == "invalid_input"


def test_redmine_opens_in_a_tab_by_default_and_falls_back_quietly(db, monkeypatch):
    """既定は「タブ」。タブにできない環境(Windows 以外)では、エラーのように知らせず、別のウィンドウ→ブラウザの順に戻る。"""
    from remi.context import AppContext
    from remi.server.api import NullWindow, RemiApi

    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    api = RemiApi(AppContext(db), window=NullWindow())
    assert api.getSettings()["redmine_open_in"] == "tab"
    sub = api.ctx.bus.subscribe()
    assert api.openRedmine("https://rm.example/issues/1") == "browser"
    assert sub.get(timeout=0.05) is None  # 「タブで開けなかった」とは知らせない
    # タブを使える環境では、設定を変えなくても、タブで開く
    api2 = RemiApi(AppContext(db), window=NullWindow(simulate_tabs=True))
    assert api2.openRedmine("https://rm.example/issues/2") == "tab"
    assert opened == ["https://rm.example/issues/1"]


def test_tab_failure_reason_is_announced(db, monkeypatch):
    """タブにできなかったときは、別ウィンドウで開き、その理由を画面に知らせる。"""
    from remi.context import AppContext
    from remi.server.api import NullWindow, RemiApi
    from remi.system.tabs import TabController

    class Broken:
        def open(self, tab):
            raise RuntimeError("boom")

    class Win(NullWindow):
        def open_redmine(self, url):
            return True

    win = Win(simulate_tabs=True)
    win.tabs = TabController(host=Broken())
    api = RemiApi(AppContext(db), window=win)
    sub = api.ctx.bus.subscribe()
    api.setSetting("redmine_open_in", "tab")
    assert api.openRedmine("https://rm.example/issues/1") == "app"
    events = []
    while (e := sub.get(timeout=0.05)) is not None:
        events.append(e)
    assert [e["reason"] for e in events if e["type"] == "tabsUnavailable"] == ["RuntimeError: boom"]
    # 設定の画面に出す理由(Windows でタブを使えなかったとき)
    win.tabs_error = "ImportError: no WebView2"
    assert api.bootstrap()["tabsError"] == "ImportError: no WebView2"


def test_tabs_fall_back_when_not_supported(db, monkeypatch):
    from remi.context import AppContext
    from remi.server.api import NullWindow, RemiApi

    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)

    class Win(NullWindow):
        def open_redmine(self, url):
            return True  # 別ウィンドウは使える

    api = RemiApi(AppContext(db), window=Win())
    assert api.bootstrap()["tabsSupported"] is False
    api.setSetting("redmine_open_in", "tab")
    assert api.openRedmine("https://rm.example/issues/1") == "app"  # タブにできなければ、別ウィンドウ
    assert api.getTabs() == {"tabs": [], "active": None}
    for call in (lambda: api.activateTab(1), lambda: api.closeTab(1), lambda: api.detachTab(1)):
        with pytest.raises(Exception) as err:
            call()
        assert err.value.code == "not_supported"
    assert opened == []


def _save(app, fake, key, server_id=None, name="社内"):
    probe = app.rpc("testConnection", url=fake.base_url, apiKey=key, serverId=server_id)
    return app.rpc("saveServer", data={
        "serverId": server_id, "name": name, "url": fake.base_url, "apiKey": key, "textFormat": "common_mark",
        "pollInterval": 180, "userId": probe["userId"], "login": probe["login"], "userName": probe["name"],
        "groupIds": probe["groupIds"]})["serverId"]


def test_switching_user_clears_previous_users_data(app, fake):
    """同じURLで別のユーザーのキーに替えたら、前のユーザーのデータを残さない。同じユーザーなら残す。"""
    from remi.paths import attachment_cache_dir

    fake.add_project(1, "基幹")
    fake.add_issue(10, 1, "Aの担当", description="Aだけが見るチケット")
    fake.add_journal(10, 7, "Aへのプライベートノート", private=True)
    fake.add_attachment(10, 77, "shot.png", b"\x89PNG-data")
    fake.add_issue(11, 1, "田中さんの担当", assigned_to=7)
    fake.keys.update({"key-b": 7, "key-b2": 7})

    sid = _save(app, fake, API_KEY)
    assert wait_until(lambda: app.ctx.issues.journals_synced(sid, 10))
    app.rpc("saveDraft", serverId=sid, issueId=10, body="Aの下書き")
    assert app.http.get(app.server.origin + f"/attachments/{sid}/77").status_code == 200
    assert any(attachment_cache_dir(sid).iterdir())
    assert app.ctx.issues.get(sid, 11) is None

    # 接続テストを経ていない新しいキーは保存できない(誰のキーか確かめられないため)
    err = app.rpc_error("saveServer", data={"serverId": sid, "name": "社内", "url": fake.base_url, "apiKey": "key-b",
                                            "textFormat": "common_mark", "pollInterval": 180})
    assert err["code"] == "not_tested"
    # 接続テストに失敗しても、既存のデータとキーはそのまま
    assert app.rpc_error("testConnection", url=fake.base_url, apiKey="wrong", serverId=sid)["code"] == "unauthorized"
    assert app.ctx.issues.get(sid, 10) is not None and app.ctx.servers.api_key(sid) == API_KEY

    events = []
    app.ctx.bus.on(events.append)
    assert _save(app, fake, "key-b", sid) == sid
    # 前のユーザー(A)のチケット・コメント・添付・下書き・画像キャッシュは残らない
    assert app.ctx.issues.get(sid, 10) is None
    assert app.ctx.issues.journals(sid, 10) == [] and app.ctx.issues.attachments(sid, 10) == []
    assert app.ctx.issues.get_draft(sid, 10)[0] in ("", None)
    assert not any(attachment_cache_dir(sid).iterdir())
    assert app.http.get(app.server.origin + f"/attachments/{sid}/77").status_code == 404
    assert any(e["type"] == "serverCacheCleared" and e["serverId"] == sid for e in events)
    # 新しいユーザー(B)の担当で同期し直す
    assert wait_until(lambda: app.ctx.issues.journals_synced(sid, 11))
    assert [i["issueId"] for i in app.rpc("listIssues")["issues"]] == [11]
    assert app.ctx.servers.get(sid).my_user_id == 7

    # 同じユーザー(B)のキーに替えただけなら、初期化しない
    events.clear()
    _save(app, fake, "key-b2", sid)
    assert app.ctx.issues.journals_synced(sid, 11)
    assert app.ctx.servers.get(sid).initial_sync_done
    assert not any(e["type"] == "serverCacheCleared" for e in events)


def test_loading_state_while_roadmap_is_fetched(app, fake):
    """マイルストーンの監視をオンにすると、取得している間だけ「読み込み中」になる(ツリーの印と状態欄)。"""
    fake.add_project(1, "A")
    fake.add_project(2, "A-子", parent=1)
    fake.add_version(5, 2, "v1.0")
    fake.add_issue(10, 1, "自分のチケット")
    fake.add_issue(20, 2, "関係ない", assigned_to=7, version=5)
    sid = _register(app, fake)
    assert wait_until(lambda: len(app.ctx.issues.list_issues([sid])) == 1)
    time.sleep(1.5)  # 最初の同期が落ち着くのを待つ
    tree = lambda: app.rpc("getTree")["servers"][0]  # noqa: E731
    assert tree()["loading"] is None and not tree()["projects"][0]["loadingVersions"]
    seen: list[tuple[str | None, bool]] = []
    fake.delay = 0.3  # 取得している間の印を確実に見られるよう、応答を遅らせる
    app.rpc("setProjectSetting", serverId=sid, projectId=1, key="watch_versions", value=True)
    end = time.monotonic() + 20
    while time.monotonic() < end:
        s = tree()
        seen.append((s["loading"], s["projects"][0]["loadingVersions"]))
        if (x := app.ctx.versions.get(sid, 5)) and x.issues_synced and s["loading"] is None:
            break
        time.sleep(0.05)
    # 取得している間は、サーバーとプロジェクトに読み込み中の印が付き、終わると消える
    assert ("versions", True) in seen
    assert seen[-1] == (None, False)
    # バージョンは、取得できた(設定を変えたら、バージョンの同期だけがすぐ動く)
    assert app.ctx.versions.get(sid, 5).issues_synced


def test_versions_api_list_tree_and_read(app, fake):
    """バージョンのチケットは、バージョンを選んだときだけ一覧に出す(件数・未読数には入れない)。"""
    from remi.sync.engine import ServerSync

    fake.add_project(1, "A")
    fake.add_project(2, "A-子", parent=1)
    fake.add_version(5, 2, "v1.0")
    fake.add_version(6, 2, "v0.9", status="closed")
    fake.add_issue(10, 1, "自分のチケット")
    fake.add_issue(20, 2, "関係ない", assigned_to=7, version=5)
    fake.add_issue(21, 2, "関係ない(完了)", assigned_to=7, status_id=5, version=5)
    sid = _register(app, fake)
    assert wait_until(lambda: len(app.ctx.issues.list_issues([sid])) == 1)
    app.rpc("setProjectSetting", serverId=sid, projectId=1, key="watch_versions", value=True)
    assert app.rpc("getProjectSettings", serverId=sid, projectId=2)["watch_versions"]["effective"] is True
    child = lambda: app.rpc("getTree")["servers"][0]["projects"][0]["children"][0]  # noqa: E731
    # 同期スレッドの取得が終わるのを待つ(このあと同じ処理をテストからも呼ぶので、重ならないように)
    assert wait_until(lambda: (x := app.ctx.versions.get(sid, 5)) and x.issues_synced)
    time.sleep(0.3)
    v = {x["versionId"]: x for x in child()["versions"]}
    assert v[5]["closed"] is False and v[5]["done"] == 1 and v[5]["total"] == 2 and v[5]["changed"] is False
    assert v[5]["synced"] is True
    # 終了したバージョンのチケットは、開いた(fetchVersion)ときに初めて取得する
    assert v[6]["closed"] is True and v[6]["synced"] is False
    assert app.rpc("getVersion", serverId=sid, versionId=6, markRead=False)["synced"] is False
    assert app.rpc("fetchVersion", serverId=sid, versionId=6) == {"started": True}
    assert wait_until(lambda: app.ctx.versions.get(sid, 6).issues_synced)
    assert app.rpc("fetchVersion", serverId=sid, versionId=5) == {"started": False}  # 取得済みの未完了は定期の確認に任せる
    assert app.rpc_error("fetchVersion", serverId=sid, versionId=999)["code"] == "not_found"
    tree = app.rpc("getTree")["servers"][0]
    assert tree["count"] == 1  # バージョンのチケットは件数に入れない
    assert [i["issueId"] for i in app.rpc("listIssues")["issues"]] == [10]
    # マイルストーンの「クローズ済みも表示」は、チケット一覧の設定(show_closed)とは別で、既定はオン
    vids = lambda: sorted(i["issueId"] for i in app.rpc("listIssues", serverId=sid, versionId=5)["issues"])  # noqa: E731
    assert vids() == [20, 21]
    app.rpc("setSetting", key="version_show_closed", value=False)
    assert vids() == [20]
    app.rpc("setSetting", key="show_closed", value=True)  # 一覧の設定は、バージョンには効かない
    assert vids() == [20]
    app.rpc("setSetting", key="version_show_closed", value=True)
    assert vids() == [20, 21]
    app.rpc("setSetting", key="show_closed", value=False)
    assert app.rpc_error("listIssues", serverId=sid, versionId=999)["code"] == "not_found"

    # 変化があれば印が付き、表示したら消える
    fake.add_issue(22, 2, "追加", assigned_to=7)
    fake.set_version(22, 5)
    engine = ServerSync(app.ctx.db, sid, app.sync.client(sid))
    engine.sync_versions()
    assert {x["versionId"]: x for x in child()["versions"]}[5]["changed"] is True
    d = app.rpc("getVersion", serverId=sid, versionId=5, markRead=False)
    assert [(e["kind"], e["issueId"], e["unread"]) for e in d["events"]] == [("added", 22, True)]
    assert d["breadcrumb"] == ["A", "A-子"] and d["total"] == 3
    app.rpc("getVersion", serverId=sid, versionId=5)
    assert {x["versionId"]: x for x in child()["versions"]}[5]["changed"] is False
    assert app.rpc("getVersion", serverId=sid, versionId=5)["events"][0]["unread"] is False
    # 関係ないチケットのコメントは、未読として数えない
    assert all(i["unread"] == 0 for i in app.rpc("listIssues", serverId=sid, versionId=5)["issues"])
    # ウォッチをやめるとツリーから消える(自分のチケットのない子プロジェクトも出なくなる)
    app.rpc("setProjectSetting", serverId=sid, projectId=1, key="watch_versions", value=None)

    def any_versions(nodes):
        return any(p["versions"] or any_versions(p["children"]) for p in nodes)
    assert wait_until(lambda: not any_versions(app.rpc("getTree")["servers"][0]["projects"]))
    assert app.rpc("getTree")["servers"][0]["projects"][0]["children"] == []


def test_fields_follow_the_tracker_and_role_settings(app, fake):
    """Redmineで非表示にした標準項目は出さない。親チケット・ウォッチの変更には、その権限が要る。"""
    fake.add_project(1, "A")
    fake.members[1] = [ME]
    fake.add_issue(10, 1, "x")
    fake.my_roles = {1: [3]}
    fake.roles = {3: ["view_issues", "edit_issues", "add_issue_notes", "add_issue_watchers"]}
    sid = _register(app, fake)
    assert wait_until(lambda: app.ctx.issues.get(sid, 10))
    keys = lambda: [x["key"] for x in app.rpc("getIssueDetails", serverId=sid, issueId=10)["fields"]]  # noqa: E731
    perms = lambda: app.rpc("getEditOptions", serverId=sid, issueId=10)["permissions"]  # noqa: E731

    # トラッカーの設定が分からない(古いRedmine)ときは、すべて出す
    assert "estimated_hours" in keys() and perms()["assignee"] is True
    # トラッカーの標準項目を絞ると、使わない項目は出ない(トラッカー自体は常に出る)
    fake.trackers[0]["enabled_standard_fields"] = ["start_date", "due_date", "done_ratio"]
    app.api._tracker_fields.clear()
    assert keys() == ["tracker_id", "start_date", "due_date", "done_ratio"]
    assert perms()["assignee"] is False  # 担当者を使わないトラッカー
    fake.trackers[0]["enabled_standard_fields"] = ["assigned_to_id", "parent_issue_id"]
    app.api._tracker_fields.clear()
    assert keys() == ["tracker_id", "parent_issue_id"] and perms()["assignee"] is True
    # 親チケットの変更は manage_subtasks、ウォッチの追加・解除は add_/delete_issue_watchers
    p = perms()
    assert (p["subtasks"], p["watchAdd"], p["watchRemove"]) == (False, True, False)
    fake.roles = {3: ["view_issues", "edit_issues", "manage_subtasks", "delete_issue_watchers"]}
    app.api._role_perms.clear()
    p = perms()
    assert (p["subtasks"], p["watchAdd"], p["watchRemove"]) == (True, False, True)


def test_issue_details_and_update(app, fake):
    """「詳細」の項目(標準項目とカスタムフィールド)の値・選択肢と、まとめての更新。"""
    fake.add_project(1, "A")
    fake.add_version(5, 1, "v1.0")
    fake.add_version(6, 1, "v0.9", status="closed")
    fake.categories[1] = [{"id": 3, "name": "画面"}]
    fake.custom_fields = [
        {"id": 1, "name": "顧客", "field_format": "string"},
        {"id": 2, "name": "環境", "field_format": "list", "multiple": True,
         "possible_values": [{"value": "本番"}, {"value": "検証"}]},
        {"id": 3, "name": "メモ", "field_format": "text"},
    ]
    fake.add_issue(10, 1, "担当", custom_fields=[
        {"id": 1, "name": "顧客", "value": "A社"}, {"id": 2, "name": "環境", "multiple": True, "value": ["本番"]},
        {"id": 3, "name": "メモ", "value": "長い文"}])
    sid = _register(app, fake)
    assert wait_until(lambda: app.ctx.issues.journals_synced(sid, 10))

    # 管理者でなければ、カスタムフィールドの形式が分からないので表示だけ
    d = app.rpc("getIssueDetails", serverId=sid, issueId=10)
    assert d["customFieldsKnown"] is False
    f = {x["key"]: x for x in d["fields"]}
    assert [x["key"] for x in d["fields"]][:8] == ["tracker_id", "category_id", "fixed_version_id", "parent_issue_id",
                                                  "start_date", "due_date", "done_ratio", "estimated_hours"]
    assert [o["value"] for o in f["fixed_version_id"]["options"]] == [None, 5]  # 終了したバージョンは選べない
    assert [o["label"] for o in f["category_id"]["options"]] == ["", "画面"]
    assert f["cf:1"]["editable"] is False and f["cf:1"]["reason"] == "custom_field_unknown"
    assert f["cf:1"]["display"] == "A社"

    fake.admin = True
    app.api._custom_fields.clear()
    f = {x["key"]: x for x in app.rpc("getIssueDetails", serverId=sid, issueId=10)["fields"]}
    assert f["cf:1"]["editable"] is True and f["cf:1"]["type"] == "text"
    assert f["cf:2"]["type"] == "multiselect" and f["cf:2"]["value"] == ["本番"]
    assert [o["value"] for o in f["cf:2"]["options"]] == ["本番", "検証"]
    assert f["cf:3"]["editable"] is False and f["cf:3"]["reason"] == "custom_field_unsupported"

    changes = {"category_id": 3, "fixed_version_id": 5, "start_date": "2026-12-01", "estimated_hours": 2.5,
               "done_ratio": 30, "cf:1": "B社", "cf:2": ["本番", "検証"]}
    app.rpc("postNote", serverId=sid, issueId=10, changes=changes)
    assert wait_until(lambda: fake.issues[10].get("start_date") == "2026-12-01")
    issue = fake.issues[10]
    assert issue["category"]["id"] == 3 and issue["fixed_version"]["id"] == 5 and issue["estimated_hours"] == 2.5
    assert {cf["id"]: cf["value"] for cf in issue["custom_fields"]}[1] == "B社"
    assert {cf["id"]: cf["value"] for cf in issue["custom_fields"]}[2] == ["本番", "検証"]
    # 外す(空にする)
    app.rpc("postNote", serverId=sid, issueId=10, changes={"fixed_version_id": None, "start_date": None})
    assert wait_until(lambda: "fixed_version" not in fake.issues[10] and fake.issues[10].get("start_date") is None)
    # おかしな値は断る
    for bad in ({"start_date": "12/01"}, {"done_ratio": 150}, {"estimated_hours": "x"}, {"cf:x": "a"},
                {"tracker_id": "1"}, {"subject": "件名"}):
        assert app.rpc_error("postNote", serverId=sid, issueId=10, changes=bad)["code"] == "invalid_input"


def test_edit_delete_note_and_attachment(app, fake):
    """コメントの編集・削除と添付の削除。Redmineで消えたものは、取り直したときに手元からも消す。"""
    fake.add_project(1, "A")
    fake.add_issue(10, 1, "担当")
    j1 = fake.add_journal(10, ME, "自分のコメント")
    j2 = fake.add_journal(10, ME, "変更つき", details=[
        {"property": "attr", "name": "priority_id", "old_value": "2", "new_value": "3"}])
    fake.add_attachment(10, 77, "shot.png", b"png")
    fake.add_journal(10, 7, details=[{"property": "attachment", "name": "77", "new_value": "shot.png"}])
    sid = _register(app, fake)
    assert wait_until(lambda: app.ctx.issues.journals_synced(sid, 10) and app.ctx.issues.attachments(sid, 10))
    perms = app.rpc("getEditOptions", serverId=sid, issueId=10)["permissions"]
    assert perms["editNotes"] == "own"  # 権限が分からないときは、自分のコメントだけ

    notes = lambda: {j.journal_id: (j.notes, j.private_notes) for j in app.ctx.issues.journals(sid, 10)}  # noqa: E731
    app.rpc("editNote", serverId=sid, issueId=10, journalId=j1["id"], notes="書き直した", private=True)
    assert wait_until(lambda: notes().get(j1["id"]) == ("書き直した", True))
    # 本文を空にすると削除。項目の変更もある更新は、本文だけが消える
    app.rpc("editNote", serverId=sid, issueId=10, journalId=j1["id"], notes="")
    app.rpc("editNote", serverId=sid, issueId=10, journalId=j2["id"], notes="")
    assert wait_until(lambda: j1["id"] not in notes() and notes().get(j2["id"]) == ("", False))
    assert app.rpc_error("editNote", serverId=sid, issueId=10, journalId=99999, notes="x")["code"] == "not_found"
    fake.notes_editable = False
    j3 = fake.add_journal(10, ME, "もう1つ")
    app.rpc("syncNow", serverId=sid, issueId=10)
    assert wait_until(lambda: j3["id"] in notes())
    assert app.rpc_error("editNote", serverId=sid, issueId=10, journalId=j3["id"], notes="x")["code"] == "forbidden"

    app.rpc("deleteAttachment", serverId=sid, issueId=10, attachmentId=77)
    assert 77 not in fake.attachments
    assert wait_until(lambda: not app.ctx.issues.attachments(sid, 10))
    assert app.rpc_error("deleteAttachment", serverId=sid, issueId=10, attachmentId=77)["code"] == "not_found"


def test_edit_notes_permission_follows_redmine(app, fake):
    """コメントの編集・削除は、Redmineと同じく edit_issue_notes(すべて)/ edit_own_issue_notes(自分のもの)。"""
    fake.add_project(1, "A")
    fake.add_issue(10, 1, "担当")
    fake.my_roles = {1: [3]}
    fake.roles = {3: ["view_issues", "add_issue_notes", "edit_own_issue_notes"]}
    sid = _register(app, fake)
    assert wait_until(lambda: app.ctx.issues.get(sid, 10) is not None)
    assert app.rpc("getEditOptions", serverId=sid, issueId=10)["permissions"]["editNotes"] == "own"
    fake.roles = {3: ["view_issues", "edit_issue_notes"]}
    app.api._role_perms.clear()
    assert app.rpc("getEditOptions", serverId=sid, issueId=10)["permissions"]["editNotes"] == "all"
    fake.roles = {3: ["view_issues"]}
    app.api._role_perms.clear()
    assert app.rpc("getEditOptions", serverId=sid, issueId=10)["permissions"]["editNotes"] == "none"


def test_pending_uploads_keep_only_images_within_total_limit(app, fake, monkeypatch):
    """送信前の添付は、プレビューに使う画像だけを、合計の上限までメモリに置く。"""
    from remi.server import http as local_http

    fake.add_project(1, "A")
    fake.add_issue(10, 1, "x")
    sid = _register(app, fake)
    assert wait_until(lambda: app.sync.client(sid) is not None)

    def upload(name, data):
        url = f"{app.server.origin}/upload?serverId={sid}&filename={name}"
        return app.http.post(url, data=data, headers={"X-Remi-Upload": "1"}).json()["result"]

    doc = upload("report.pdf", b"%PDF" * 100)
    assert not doc["isImage"] and doc["token"]
    assert app.server._pending == {}  # 画像以外はメモリに置かない
    monkeypatch.setattr(local_http, "PENDING_MAX_TOTAL", 25)
    a, b = upload("a.png", b"a" * 10), upload("b.png", b"b" * 10)
    c = upload("c.png", b"c" * 10)  # 合計が上限を超えるので、古いもの(a)から捨てる
    assert app.http.get(app.server.origin + a["url"]).status_code == 404
    assert app.http.get(app.server.origin + b["url"]).content == b"b" * 10
    assert app.http.get(app.server.origin + c["url"]).content == b"c" * 10
    assert app.server._pending_bytes == 20


def test_switching_user_is_cancelled_if_sync_does_not_stop(app, fake, monkeypatch):
    """同期スレッドが止まりきらなければ、前のデータを消さずに切り替えをやめる(後から書き戻されないように)。"""
    fake.add_project(1, "基幹")
    fake.add_issue(10, 1, "Aの担当")
    fake.keys["key-b"] = 7
    sid = _save(app, fake, API_KEY)
    assert wait_until(lambda: app.ctx.issues.get(sid, 10) is not None)
    real_stop = app.sync.stop_all
    monkeypatch.setattr(app.sync, "stop_all", lambda timeout=5.0: (real_stop(timeout), False)[1])
    probe = app.rpc("testConnection", url=fake.base_url, apiKey="key-b", serverId=sid)
    err = app.rpc_error("saveServer", data={
        "serverId": sid, "name": "社内", "url": fake.base_url, "apiKey": "key-b", "textFormat": "common_mark",
        "pollInterval": 180, "userId": probe["userId"], "login": probe["login"], "userName": probe["name"],
        "groupIds": probe["groupIds"]})
    assert err["code"] == "sync_busy"
    assert app.ctx.servers.get(sid).my_user_id == ME and app.ctx.servers.api_key(sid) == API_KEY
    assert app.ctx.issues.get(sid, 10) is not None
    assert app.sync.client(sid) is not None or wait_until(lambda: app.sync.client(sid) is not None)  # 同期は再開する


def test_switching_user_clears_custom_field_definitions(app, fake):
    """前のユーザー(管理者など)の権限で取得したカスタムフィールドの定義を、切り替えた後に使わない。"""
    fake.add_project(1, "基幹")
    fake.keys["key-b"] = 7
    sid = _save(app, fake, API_KEY)
    app.api._custom_fields[sid] = (time.monotonic(), [{"id": 1, "name": "管理者だけが見る項目"}])
    _save(app, fake, "key-b", sid)
    assert sid not in app.api._custom_fields


def test_new_http_connections_do_not_accumulate_db_connections(app, db):
    """ローカルHTTPサーバーは接続ごとにスレッドを作る。終了したスレッドのDB接続は閉じる(長期稼働でのリーク対策)。"""
    for _ in range(30):
        with requests.Session() as s:  # 毎回新しいTCP接続 = 新しいスレッド
            s.trust_env = False
            s.cookies.update(app.http.cookies)
            assert s.post(f"{app.server.origin}/api/listServers", json={}).status_code == 200
    time.sleep(0.2)  # 最後のハンドラースレッドが終わるのを待つ
    db.conn()
    with requests.Session() as s:
        s.trust_env = False
        s.cookies.update(app.http.cookies)
        s.post(f"{app.server.origin}/api/listServers", json={})
    assert db.open_connections < 10


def test_save_server_rejects_one_minute_poll_interval(app, fake):
    probe = app.rpc("testConnection", url=fake.base_url, apiKey=API_KEY)
    err = app.rpc_error("saveServer", data={
        "name": "S", "url": fake.base_url, "apiKey": API_KEY, "textFormat": "common_mark",
        "pollInterval": 60, "userId": probe["userId"], "login": probe["login"], "userName": probe["name"]})
    assert err["code"] == "invalid_input"


def test_same_definition_is_fetched_once_when_requested_at_the_same_time(app, fake):
    """「詳細」と編集の選択肢が同時に同じ定義(トラッカー・カスタムフィールド)を求めても、取得は1回だけ。"""
    fake.add_project(1, "A")
    fake.add_issue(10, 1, "x")
    sid = _register(app, fake)
    assert wait_until(lambda: app.ctx.issues.get(sid, 10) is not None)
    fake.delay = 0.2  # 同時に来たものが重なるように、応答を遅らせる
    before = len(fake.requests)
    threads = [threading.Thread(target=lambda: app.api._enabled_fields(sid, 1)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    paths = [p.split("?")[0] for _m, p in fake.requests[before:]]
    assert paths.count("/trackers.json") == 1
