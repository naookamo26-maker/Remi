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
