"""全体の点検で見つかった不具合の確認(0.23.0)。"""

import time

from fake_redmine import API_KEY, ME
from test_server import _save, app, wait_until  # noqa: F401 - app はフィクスチャ
from test_sync_worker import Recorder

from remi.events import EventBus
from remi.render.markup import RenderContext, render
from remi.store.issues import IssueStore
from remi.store.servers import Server, ServerStore
from remi.sync.worker import SyncManager


# --- 画像の添付名(Markdown のエンコード) -------------------------------------------------
def test_markdown_image_with_space_or_japanese_name_resolves_to_attachment():
    ctx = RenderContext(server_id=1, server_url="https://r", text_format="common_mark",
                        attachments={"my image.png": 5, "スクリーンショット 2026-11-21.png": 6, "a%20b.png": 7})
    assert 'src="/attachments/1/5"' in render("![](<my image.png>)", ctx)
    assert 'src="/attachments/1/6"' in render("![](<スクリーンショット 2026-11-21.png>)", ctx)
    assert 'src="/attachments/1/7"' in render("![](a%20b.png)", ctx)  # 名前に % を含むものは、そのまま一致させる
    ctx.pending = {"送信前.png": "/pending/3"}
    assert 'src="/pending/3"' in render("![](送信前.png)", ctx)


# --- 未読に戻す ---------------------------------------------------------------------
def _issue_with_notes(db, notes):
    s = ServerStore(db).save(Server(name="S", url="http://x", my_user_id=ME), api_key="k")
    st = IssueStore(db)
    st.upsert_issue(s.server_id, {"id": 1, "project": {"id": 1}, "subject": "x",
                                  "updated_on": "2026-01-01T00:00:00Z"}, 1)
    st.upsert_journals(s.server_id, 1, [{"id": 10 + i, "user": {"id": u}, "notes": n} for i, (u, n) in enumerate(notes)])
    st.mark_read(s.server_id, 1)
    return s.server_id, st


def test_mark_unread_goes_back_to_the_last_comment_of_others(db):
    sid, st = _issue_with_notes(db, [(7, "他の人"), (ME, "自分の返信")])
    assert st.mark_unread(sid, 1)
    assert st.unread_count(sid, 1) == 1


def test_mark_unread_does_nothing_without_comments_of_others(db):
    sid, st = _issue_with_notes(db, [(ME, "自分だけ")])
    before = st.last_read(sid, 1)
    assert not st.mark_unread(sid, 1)
    assert st.last_read(sid, 1) == before


def test_list_tells_whether_mark_unread_is_possible(app, fake):
    fake.add_project(1, "A")
    fake.add_issue(10, 1, "他の人のコメントあり")
    fake.add_journal(10, 7, "どうですか")
    fake.add_issue(11, 1, "自分のコメントだけ")
    fake.add_journal(11, ME, "メモ")
    sid = _save(app, fake, API_KEY)
    assert wait_until(lambda: all(app.ctx.issues.journals_synced(sid, i) for i in (10, 11)))
    items = {i["issueId"]: i for i in app.rpc("listIssues")["issues"]}
    assert items[10]["canMarkUnread"] and not items[11]["canMarkUnread"]
    assert app.rpc("markUnread", serverId=sid, issueId=10) is True
    assert app.rpc("markUnread", serverId=sid, issueId=11) is False
    items = {i["issueId"]: i for i in app.rpc("listIssues")["issues"]}
    assert items[10]["unread"] == 1 and not items[10]["canMarkUnread"]  # すでに未読


# --- 検索の % と _ ----------------------------------------------------------------------
def test_search_treats_percent_and_underscore_literally(db):
    s = ServerStore(db).save(Server(name="S", url="http://x"), api_key="k")
    st = IssueStore(db)
    for iid, subject in ((1, "進捗100%"), (2, "進捗1000"), (3, "file_name"), (4, "filexname")):
        st.upsert_issue(s.server_id, {"id": iid, "project": {"id": 1}, "subject": subject}, 1)
    assert [r.issue_id for r in st.list_issues(search="100%")] == [1]
    assert [r.issue_id for r in st.list_issues(search="file_")] == [3]


# --- クローズ済みのチケットのコメント --------------------------------------------------------
def test_comment_on_closed_issue_is_not_prefetched_or_notified(db, fake):
    fake.add_project(1, "A")
    fake.add_issue(1, 1, "担当")
    sid = ServerStore(db).save(Server(name="S", url=fake.base_url), api_key=API_KEY).server_id
    bus = EventBus()
    rec = Recorder(bus)
    mgr = SyncManager(db, bus)
    mgr.restart()
    store = IssueStore(db)
    try:
        assert wait_until(lambda: store.get(sid, 1) is not None and store.journals_synced(sid, 1))
        time.sleep(1.1)  # updated_on は秒単位
        fake.set_status(1, 5)  # ほかの人がクローズして、コメントを書いた
        fake.add_journal(1, 7, "クローズしました")
        mgr.sync_now()
        assert wait_until(lambda: not store.get(sid, 1).monitored)
        time.sleep(1.5)  # 先読みの順番待ちが回るのを待つ
        assert rec.of("newComments") == []
        assert not any(j.notes == "クローズしました" for j in store.journals(sid, 1))  # 開いたときに取得する
        # 開けば取得する(通知はしない)
        mgr.set_open_issue(sid, 1)
        assert wait_until(lambda: any(j.notes == "クローズしました" for j in store.journals(sid, 1)))
        assert rec.of("newComments") == []
    finally:
        mgr.stop_all()


def test_watching_a_closed_issue_does_not_make_it_monitored(db):
    from remi.store.masters import MasterStore
    from remi.sync.engine import ServerSync

    sid = ServerStore(db).save(Server(name="S", url="http://x"), api_key="k").server_id
    MasterStore(db).replace(sid, "status", [{"id": 5, "name": "終了", "is_closed": True}], extra_keys=("is_closed",))
    st = IssueStore(db)
    st.upsert_issue(sid, {"id": 1, "project": {"id": 1}, "subject": "x", "status": {"id": 5}}, 0, monitored=False)
    ServerSync(db, sid, client=None).set_watching_flag(1, True)
    row = st.get(sid, 1)
    assert row.relation_flags and not row.monitored


def test_notifier_skips_unmonitored_issues(db):
    from test_notify_tray import comment, setup

    ctx, sid, n, backend, api, opened = setup(db)
    ctx.issues.upsert_issue(sid, {"id": 7, "project": {"id": 1}, "subject": "クローズ済み"}, 1, monitored=False)
    ctx.bus.publish("newComments", serverId=sid, comments=[comment(7)])
    assert backend.shown == []


# --- 接続先・ユーザーの切り替え ---------------------------------------------------------------
def test_switching_server_clears_notified_comments(app, fake):
    fake.add_project(1, "A")
    fake.add_issue(10, 1, "担当")
    fake.add_journal(10, 7, "コメント")
    fake.keys.update({"key-b": 7})
    sid = _save(app, fake, API_KEY)
    assert wait_until(lambda: app.ctx.issues.journals_synced(sid, 10))
    app.ctx.issues.set_notified(sid, 10, 99999)
    _save(app, fake, "key-b", sid)  # 別のユーザー: キャッシュとともに、通知の記録も捨てる
    assert app.ctx.issues.last_notified(sid, 10) == 0


def test_saved_key_is_not_sent_to_another_server(app, fake):
    fake.add_project(1, "A")
    sid = _save(app, fake, API_KEY)
    err = app.rpc_error("testConnection", url="https://other.example.com", apiKey="", serverId=sid)
    assert err["code"] == "key_required"
    err = app.rpc_error("saveServer", data={"serverId": sid, "name": "社内", "url": "https://other.example.com",
                                            "textFormat": "common_mark", "pollInterval": 180})
    assert err["code"] == "key_required"
    # 同じ接続先なら、保存済みのキーで接続テストできる
    assert app.rpc("testConnection", url=fake.base_url, apiKey="", serverId=sid)["userId"] == ME
    assert app.rpc_error("saveServer", data={"serverId": sid, "name": "x", "url": fake.base_url,
                                             "pollInterval": "abc"})["code"] == "invalid_input"


# --- 送信の結果 ------------------------------------------------------------------------
def test_send_result_can_be_asked_again(app, fake):
    fake.add_project(1, "A")
    fake.add_issue(10, 1, "担当")
    sid = _save(app, fake, API_KEY)
    assert wait_until(lambda: app.ctx.issues.journals_synced(sid, 10))
    assert app.rpc("getSendResult", serverId=sid, issueId=10) == {"pending": False, "result": None}
    app.rpc("postNote", serverId=sid, issueId=10, body="返信")
    # 結果は noteFinished で届くが、受け取り損ねても問い合わせ直せる
    got = []
    assert wait_until(lambda: (r := app.rpc("getSendResult", serverId=sid, issueId=10)) and not r["pending"]
                      and got.append(r) is None)
    assert got[0]["result"]["error"] is None


def test_send_result_after_note_finished(db):
    from remi.context import AppContext
    from remi.server.api import RemiApi

    ctx = AppContext(db)
    api = RemiApi(ctx, None)
    api._sends[(1, 2)] = None
    assert api.getSendResult(1, 2) == {"pending": True, "result": None}
    ctx.bus.publish("noteFinished", serverId=1, issueId=2, error="forbidden", detail="x")
    assert api.getSendResult(1, 2) == {"pending": False,
                                       "result": {"error": "forbidden", "detail": "x", "uploadTokens": []}}
    assert api.getSendResult(1, 2) == {"pending": False, "result": None}  # 受け取ったら消える


def test_requests_are_refused_when_the_worker_is_not_running(db, monkeypatch):
    """同期スレッドが始められなかった(APIキーを読めない)サーバーには、送信を受け付けない(結果が届かないため)。"""
    from remi.sync import worker

    sid = ServerStore(db).save(Server(name="S", url="http://x"), api_key="k").server_id
    monkeypatch.setattr(worker, "get_api_key", lambda _sid: None)
    mgr = SyncManager(db, EventBus())
    mgr.restart()
    try:
        assert wait_until(lambda: not mgr._workers[sid].is_alive())
        assert mgr.post_note(sid, 1, "x", False) is False
        assert mgr.client(sid) is None
    finally:
        mgr.stop_all()


def test_upload_over_the_limit_is_answered_in_json(app):
    from remi.server.api import UPLOAD_MAX

    import json
    import socket

    # 本文は送らない(サーバーは長さを見て、読まずに断る)。requests は本文を送ろうとして、Windows では
    # 切られた接続のエラーになるので、要求の先頭だけをソケットで送って、応答を読む
    cookie = "; ".join(f"{k}={v}" for k, v in app.http.cookies.items())
    with socket.create_connection(("127.0.0.1", app.server.port), timeout=10) as sock:
        sock.sendall((f"POST /upload?serverId=1&filename=a.bin HTTP/1.1\r\nHost: 127.0.0.1:{app.server.port}\r\n"
                      f"Cookie: {cookie}\r\nX-Remi-Upload: 1\r\nContent-Length: {UPLOAD_MAX + 1}\r\n\r\n").encode())
        raw = b""
        while chunk := sock.recv(65536):  # 断ったあとは、サーバーが接続を閉じる
            raw += chunk
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 413")
    assert json.loads(body)["error"]["code"] == "too_large"
    assert app.rpc("bootstrap")["uploadMax"] == UPLOAD_MAX
