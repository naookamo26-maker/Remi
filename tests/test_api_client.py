import time

import pytest

from fake_redmine import ME

from remi.api.client import RedmineClient, normalize_url
from remi.api.errors import ErrorCode, RedmineError
from remi.api.probe import guess_text_format, probe_server


def test_normalize_url():
    assert normalize_url("redmine.example.com/") == "https://redmine.example.com"
    assert normalize_url(" http://a/b/ ") == "http://a/b"
    with pytest.raises(RedmineError) as e:
        normalize_url("")
    assert e.value.code == ErrorCode.INVALID_URL
    with pytest.raises(RedmineError):
        normalize_url("ftp://x")


def test_current_user_and_headers(fake, client):
    u = client.current_user()
    assert u["id"] == ME
    assert client.session.headers["User-Agent"].startswith("Remi/")


def test_wrong_key_is_unauthorized(fake):
    c = RedmineClient(fake.base_url, "wrong", min_interval=0)
    with pytest.raises(RedmineError) as e:
        c.current_user()
    assert e.value.code == ErrorCode.UNAUTHORIZED


def test_connection_refused():
    c = RedmineClient("http://127.0.0.1:1", "k", min_interval=0, timeout=(1, 1))
    with pytest.raises(RedmineError) as e:
        c.current_user()
    assert e.value.code in (ErrorCode.CONNECTION_FAILED, ErrorCode.TIMEOUT)
    assert e.value.retryable


def test_server_error_is_retryable(fake, client):
    fake.fail_next = 503
    with pytest.raises(RedmineError) as e:
        client.issue_statuses()
    assert e.value.code == ErrorCode.SERVER_ERROR and e.value.retryable


def test_paging(fake, client):
    for i in range(1, 251):
        fake.add_project(i, f"P{i}")
    assert len(client.projects()) == 250
    assert sum(1 for m, p in fake.requests if p.startswith("/projects.json")) == 3


def test_throttle(fake):
    c = RedmineClient(fake.base_url, "test-key", min_interval=0.2)
    t = time.monotonic()
    c.issue_statuses()
    c.issue_statuses()
    c.issue_statuses()
    assert time.monotonic() - t >= 0.4


def test_add_note_and_validation(fake, client):
    fake.add_project(1, "A")
    fake.add_issue(10, 1, "x")
    client.add_note(10, "hello", private=True)
    j = fake.journals[10][-1]
    assert j["notes"] == "hello" and j["private_notes"] is True
    with pytest.raises(RedmineError) as e:
        client.add_note(10, "  ")
    assert e.value.code == ErrorCode.UNPROCESSABLE


def test_download_limits(fake, client, tmp_path):
    fake.add_project(1, "A")
    fake.add_issue(10, 1, "x")
    fake.add_attachment(10, 5, "a.png", b"x" * 1000)
    url = f"{fake.base_url}/attachments/download/5/a.png"
    assert client.download(url, tmp_path / "a.png", 2000) == 1000
    assert (tmp_path / "a.png").read_bytes() == b"x" * 1000
    with pytest.raises(RedmineError) as e:
        client.download(url, tmp_path / "b.png", 10)
    assert e.value.code == ErrorCode.TOO_LARGE
    assert not (tmp_path / "b.png").exists()
    assert not list(tmp_path.glob("*.part"))


def test_probe_detects_rest_disabled(fake, client):
    fake.rest_enabled = False
    with pytest.raises(RedmineError) as e:
        probe_server(client)
    assert e.value.code == ErrorCode.REST_DISABLED


def test_probe_returns_user_counts_and_format(fake, client):
    fake.add_project(1, "A")
    fake.add_issue(10, 1, "x", description="```python\nprint(1)\n```")
    fake.add_issue(11, 1, "y", assigned_to=7, watchers={ME})
    fake.add_journal(10, 7, "see ![](a.png) and **bold**")
    r = probe_server(client)
    assert r.user_id == ME and r.login == "okamo" and r.name == "岡本 ナオ"
    assert r.group_ids == [100]
    assert r.assigned_count == 1 and r.watched_count == 1
    assert r.format_guess.text_format == "common_mark"


@pytest.mark.parametrize("texts,expected", [
    (["h2. 見出し\n\n<pre>code</pre>", "!shot.png!"], "textile"),
    (["```\ncode\n```", "# Title"], "common_mark"),
    (["ただの文章です"], None),
    ([], None),
])
def test_guess_text_format(texts, expected):
    assert guess_text_format(texts).text_format == expected


def test_update_issue_with_changes_and_upload(fake, client):
    fake.add_project(1, "A")
    fake.add_issue(10, 1, "x")
    token = client.upload(b"\x89PNG", "clip.png")
    client.update_issue(10, notes="添付します", changes={"status_id": 2, "assigned_to_id": 7, "priority_id": 3},
                        uploads=[{"token": token, "filename": "clip.png", "content_type": "image/png"}])
    j = fake.journals[10][-1]
    assert j["notes"] == "添付します"
    names = {d["name"] for d in j["details"] if d["property"] == "attr"}
    assert names == {"status_id", "assigned_to_id", "priority_id"}
    assert any(d["property"] == "attachment" and d["new_value"] == "clip.png" for d in j["details"])
    assert fake.issues[10]["assigned_to"]["id"] == 7
    client.update_issue(10, changes={"assigned_to_id": None})
    assert "assigned_to" not in fake.issues[10]
    with pytest.raises(ValueError):
        client.update_issue(10, changes={"subject": "x"})


def test_upload_too_large(fake, client):
    fake.max_upload = 3
    with pytest.raises(RedmineError) as e:
        client.upload(b"12345", "a.bin")
    assert e.value.code == ErrorCode.UNPROCESSABLE


def test_edit_options_and_watchers(fake, client):
    fake.add_project(1, "A")
    fake.members[1] = [ME, 7]
    fake.add_issue(10, 1, "x", assigned_to=7)
    assert [s["id"] for s in client.allowed_statuses(10)] == [1, 2, 5]
    fake.allowed_statuses_supported = False
    assert client.allowed_statuses(10) is None
    assert {m["user"]["id"] for m in client.memberships(1)} == {ME, 7}
    client.add_watcher(10, ME)
    assert ME in fake.watchers[10]
    client.remove_watcher(10, ME)
    assert ME not in fake.watchers[10]


# --- APIキーを登録したRedmine以外へ送らない ------------------------------------------
@pytest.fixture
def redirector():
    """Redmine役(/to-other で別ホストへ、/to-self で自分へリダイレクトする)と、別ホスト役のサーバー。
    別ホスト役には localhost の名前で接続させる(127.0.0.1 とはホストとして別)。"""
    import http.server
    import threading

    got: dict[str, list] = {"other": [], "self": []}

    class Other(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            got["other"].append(self.headers.get("X-Redmine-API-Key"))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Other)

    class Red(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/to-other"):
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{other.server_port}/x")
            elif self.path.startswith("/to-self"):
                self.send_response(302)
                self.send_header("Location", "/final?a=1")
            else:
                got["self"].append((self.path, self.headers.get("X-Redmine-API-Key")))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_PUT(self):  # noqa: N802
            self.send_response(307)
            self.send_header("Location", "/final")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    red = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Red)
    for s in (other, red):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    c = RedmineClient(f"http://127.0.0.1:{red.server_port}", "SECRET", min_interval=0,
                      proxy_mode="manual", proxy_url=None)
    c.session.trust_env = False  # 環境のプロキシを使わない
    yield c, got, other.server_port
    c.close()
    for s in (other, red):
        s.shutdown()
        s.server_close()


def test_api_key_is_not_sent_to_other_host_on_redirect(redirector, tmp_path):
    c, got, _ = redirector
    with pytest.raises(RedmineError) as e:
        c.download(c.url("/to-other"), tmp_path / "a.png", 100)
    assert e.value.code == ErrorCode.OTHER_HOST
    assert got["other"] == []


def test_api_key_is_not_sent_to_other_absolute_url(redirector):
    c, got, port = redirector
    with pytest.raises(RedmineError) as e:
        c.get_json(f"http://localhost:{port}/attachments/download/1/a.png")
    assert e.value.code == ErrorCode.OTHER_HOST
    assert got["other"] == []


def test_redirect_within_same_server_is_followed(redirector):
    c, got, _ = redirector
    assert c.get_json("/to-self", {"b": 2}) == {}
    assert got["self"] == [("/final?a=1", "SECRET")]  # 移動先のクエリを使い、元のクエリは付け直さない


def test_redirect_of_update_is_not_followed(redirector):
    c, got, _ = redirector
    with pytest.raises(RedmineError) as e:
        c.request("PUT", "/issues/1.json", json={})
    assert e.value.code == ErrorCode.INVALID_RESPONSE
    assert got["self"] == []


def test_same_server():
    from remi.api.client import same_server

    assert same_server("https://r.example.com/redmine", "https://R.example.com:443/redmine/x")
    assert same_server("http://r.example.com", "https://r.example.com/x")  # https への移動は許す
    assert not same_server("https://r.example.com", "http://r.example.com/x")  # 暗号化しない接続へは送らない
    assert not same_server("https://r.example.com", "https://evil.example.com/x")
    assert not same_server("https://r.example.com", "https://r.example.com:8443/x")
    assert not same_server("https://r.example.com", "https://r.example.com.evil.com/x")


def test_attachment_url_uses_registered_url():
    c = RedmineClient("https://r.example.com/redmine", "k")
    assert c.attachment_url(5, "スクリーン ショット#1.png") == \
        "https://r.example.com/redmine/attachments/download/5/%E3%82%B9%E3%82%AF%E3%83%AA%E3%83%BC%E3%83%B3%20" \
        "%E3%82%B7%E3%83%A7%E3%83%83%E3%83%88%231.png"


def test_cancelled_client_sends_nothing(fake, client):
    client.cancel()
    n = len(fake.requests)
    with pytest.raises(RedmineError) as e:
        client.issue_statuses()
    assert e.value.code == ErrorCode.CANCELLED and not e.value.retryable
    assert len(fake.requests) == n


# --- 一時的な通信の失敗・流量制限(多数の利用者でサーバーを叩きすぎない) --------------------------------
def test_unexpected_requests_errors_are_retryable_connection_failures(client, monkeypatch):
    """応答の受信中に切れた(ChunkedEncodingError)なども、再試行する通信の失敗として扱う。"""
    import requests

    def broken(*args, **kwargs):
        raise requests.exceptions.ChunkedEncodingError("connection broken")

    monkeypatch.setattr(client.session, "request", broken)
    with pytest.raises(RedmineError) as e:
        client.issue_statuses()
    assert e.value.code == ErrorCode.CONNECTION_FAILED and e.value.retryable


def test_download_interrupted_midway_is_retryable(fake, client, tmp_path, monkeypatch):
    import requests

    fake.add_project(1, "A")
    fake.add_issue(10, 1, "x")
    fake.add_attachment(10, 5, "a.png", b"x" * 1000)

    def interrupted(self, chunk_size=1, decode_unicode=False):
        yield b"x" * 10
        raise requests.exceptions.ConnectionError("read timed out")

    monkeypatch.setattr(requests.Response, "iter_content", interrupted)
    with pytest.raises(RedmineError) as e:
        client.download(f"{fake.base_url}/attachments/download/5/a.png", tmp_path / "a.png", 2000)
    assert e.value.code == ErrorCode.CONNECTION_FAILED and e.value.retryable
    assert not (tmp_path / "a.png").exists()
    assert not list(tmp_path.glob("*.part"))


def test_rate_limited_with_retry_after(fake, client):
    fake.fail_next = 429
    fake.fail_headers = {"Retry-After": "120"}
    with pytest.raises(RedmineError) as e:
        client.issue_statuses()
    assert e.value.code == ErrorCode.RATE_LIMITED and e.value.retryable
    assert e.value.retry_after == 120


def test_server_error_with_retry_after_date(fake, client):
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    fake.fail_next = 503
    fake.fail_headers = {"Retry-After": format_datetime(datetime.now(UTC) + timedelta(seconds=300), usegmt=True)}
    with pytest.raises(RedmineError) as e:
        client.issue_statuses()
    assert e.value.code == ErrorCode.SERVER_ERROR
    assert 290 <= e.value.retry_after <= 300


@pytest.mark.parametrize("value,expected", [
    (None, None), ("", None), ("abc", None), ("0", 0.0), ("-5", 0.0), ("86400", 3600.0),
    ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0),  # 過去の日時: すぐ再試行してよい
])
def test_parse_retry_after(value, expected):
    from remi.api.client import parse_retry_after

    assert parse_retry_after(value) == expected


def test_issues_signature(fake, client):
    fake.add_project(1, "A")
    fake.add_issue(10, 1, "a", updated_on="2026-01-01T00:00:00Z")
    fake.add_issue(11, 1, "b", updated_on="2026-01-02T00:00:00Z")
    assert client.issues_signature(project_id=1, status_id="*") == "2|2026-01-02T00:00:00Z"
    assert client.issues_signature(project_id=99, status_id="*") == "0|"
    assert fake.requests[-1][1].count("limit=1") == 1  # 1件だけ取得する軽い問い合わせ
