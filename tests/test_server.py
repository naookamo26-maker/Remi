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
