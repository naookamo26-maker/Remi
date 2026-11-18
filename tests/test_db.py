import sqlite3
import threading

import pytest

from remi.store.db import Database, SchemaTooNewError, open_database
from remi.store.migrations import SCHEMA_VERSION
from remi.store.settings import Settings


def test_migrate_creates_schema(db):
    assert db.schema_version() == SCHEMA_VERSION
    tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("servers", "projects", "project_settings", "issues", "journals",
              "attachments", "read_state", "drafts", "masters", "settings"):
        assert t in tables


def test_wal_mode(db):
    assert db.query_one("PRAGMA journal_mode")[0].lower() == "wal"


def test_migrate_is_idempotent(tmp_path):
    p = tmp_path / "a.db"
    open_database(p).close_all()
    d = open_database(p)
    assert d.schema_version() == SCHEMA_VERSION
    d.close_all()


def test_newer_schema_is_rejected(tmp_path):
    p = tmp_path / "b.db"
    c = sqlite3.connect(p)
    c.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    c.close()
    with pytest.raises(SchemaTooNewError):
        Database(p).migrate()


def test_tx_rolls_back(db):
    with pytest.raises(RuntimeError):
        with db.tx() as c:
            c.execute("INSERT INTO settings(key, value) VALUES('x', '1')")
            raise RuntimeError
    assert db.query_one("SELECT * FROM settings WHERE key='x'") is None


def test_cascade_delete(db):
    with db.tx() as c:
        c.execute("INSERT INTO servers(name, url) VALUES('a', 'http://a')")
        sid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO issues(server_id, issue_id, project_id) VALUES(?, 1, 1)", (sid,))
        c.execute("DELETE FROM servers WHERE server_id=?", (sid,))
    assert db.query_one("SELECT COUNT(*) FROM issues")[0] == 0


def test_connection_per_thread(db):
    main = db.conn()
    other = []
    t = threading.Thread(target=lambda: other.append(db.conn()))
    t.start()
    t.join()
    assert other[0] is not main


def test_settings_defaults_and_roundtrip(db):
    s = Settings(db)
    assert s.get("theme") == "system"
    s.set("theme", "dark")
    assert s.get("theme") == "dark"
    s.set("tree_expanded", ["1:2", "1:3"])
    assert s.get("tree_expanded") == ["1:2", "1:3"]


def test_migrate_v2_moves_global_view_settings(tmp_path):
    """「すべてのプロジェクトを表示」と「表示するチケット」は、すべてのサーバーの設定に引き継ぐ。"""
    import json

    from remi.store.migrations import MIGRATIONS

    p = tmp_path / "v1.db"
    c = sqlite3.connect(p)
    c.executescript(MIGRATIONS[0])
    c.execute("PRAGMA user_version = 1")
    c.execute("INSERT INTO servers(server_id, name, url) VALUES(1, 'S', 'http://s')")
    c.execute("INSERT INTO projects(server_id, project_id, parent_id, name) VALUES(1, 1, NULL, 'A')")
    c.execute("INSERT INTO projects(server_id, project_id, parent_id, name) VALUES(1, 2, 1, 'B')")
    c.execute("INSERT INTO project_settings(server_id, project_id, notify) VALUES(1, 1, 0)")
    c.execute("INSERT INTO settings(key, value) VALUES('show_all_projects', 'true')")
    c.execute("INSERT INTO settings(key, value) VALUES('visible_relations', ?)",
              (json.dumps(["assigned", "group"]),))
    c.commit()
    c.close()
    d = open_database(p)
    try:
        from remi.store.projects import ProjectStore
        from remi.store.servers import ServerStore

        server = ServerStore(d).get(1)
        assert server.show_all_projects is True
        assert server.visible_relations == ["assigned", "group"]  # サーバーの既定になる
        projects = ProjectStore(d)
        assert projects.own_visible_relations(1) == {}
        assert projects.get_setting(1, 1, "notify") is False  # ほかのプロジェクト設定は残る
        assert Settings(d).get("visible_relations") is None
    finally:
        d.close_all()


def test_migrate_v2_keeps_defaults(tmp_path):
    from remi.store.migrations import MIGRATIONS

    p = tmp_path / "v1b.db"
    c = sqlite3.connect(p)
    c.executescript(MIGRATIONS[0])
    c.execute("PRAGMA user_version = 1")
    c.execute("INSERT INTO servers(server_id, name, url) VALUES(1, 'S', 'http://s')")
    c.execute("INSERT INTO projects(server_id, project_id, parent_id, name) VALUES(1, 1, NULL, 'A')")
    c.execute("""INSERT INTO settings(key, value) VALUES('visible_relations', '["assigned", "group", "watching", "authored"]')""")
    c.commit()
    c.close()
    d = open_database(p)
    try:
        from remi.store.projects import ProjectStore
        from remi.store.servers import ServerStore

        assert ServerStore(d).get(1).show_all_projects is False
        assert ServerStore(d).get(1).visible_relations is None
        assert ProjectStore(d).own_visible_relations(1) == {}
    finally:
        d.close_all()


def test_connections_of_finished_threads_are_closed(db):
    """スレッドごとの接続は、スレッドが終了したら閉じる(常駐している間に増え続けない)。"""
    db.conn()
    for _ in range(20):
        t = threading.Thread(target=lambda: db.query("SELECT 1"))
        t.start()
        t.join()
    db.conn()  # 新しい接続を作るときに、終了したスレッドのものを閉じる
    t = threading.Thread(target=lambda: db.query("SELECT 1"))
    t.start()
    t.join()
    assert db.open_connections <= 2  # メインスレッドと、最後に終了したスレッドの分


def test_connection_of_finished_thread_is_really_closed(db):
    got = []
    t = threading.Thread(target=lambda: got.append(db.conn()))
    t.start()
    t.join()
    threading.Thread(target=lambda: db.query("SELECT 1")).start()  # 別のスレッドが接続を作ると閉じられる
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            got[0].execute("SELECT 1")
        except sqlite3.ProgrammingError:
            return  # 閉じられた
        time.sleep(0.01)
    raise AssertionError("connection of a finished thread was not closed")


def test_migrate_v4_drops_one_minute_poll_interval(tmp_path):
    """ポーリング間隔の「1分」は廃止したので、3分にする。ほかの間隔はそのまま。"""
    from remi.store.migrations import MIGRATIONS
    from remi.store.servers import POLL_INTERVALS, ServerStore

    p = tmp_path / "v3.db"
    c = sqlite3.connect(p)
    for m in MIGRATIONS[:3]:
        c.executescript(m)
    c.execute("PRAGMA user_version = 3")
    c.execute("INSERT INTO servers(server_id, name, url, poll_interval) VALUES(1, 'S', 'http://s', 60)")
    c.execute("INSERT INTO servers(server_id, name, url, poll_interval) VALUES(2, 'T', 'http://t', 300)")
    c.commit()
    c.close()
    d = open_database(p)
    try:
        servers = ServerStore(d)
        assert servers.get(1).poll_interval == 180
        assert servers.get(2).poll_interval == 300
        assert 60 not in POLL_INTERVALS
    finally:
        d.close_all()
