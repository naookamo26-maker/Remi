"""DBスキーマのマイグレーション。

スキーマのバージョンはアプリのバージョンとは別に PRAGMA user_version で管理する
(設計書 §11)。新しい変更は MIGRATIONS の末尾に追加するだけにし、既存の要素は書き換えない。
"""

from __future__ import annotations

V1 = """
CREATE TABLE servers (
    server_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL,
    url               TEXT    NOT NULL,
    enabled           INTEGER NOT NULL DEFAULT 1,
    poll_interval     INTEGER NOT NULL DEFAULT 180,
    notify_enabled    INTEGER NOT NULL DEFAULT 1,
    text_format       TEXT    NOT NULL DEFAULT 'common_mark',
    proxy_mode        TEXT    NOT NULL DEFAULT 'system',
    proxy_url         TEXT,
    my_user_id        INTEGER,
    my_login          TEXT,
    my_name           TEXT,
    my_group_ids      TEXT    NOT NULL DEFAULT '[]',
    last_sync_cursor  TEXT,
    initial_sync_done INTEGER NOT NULL DEFAULT 0,
    last_synced_at    TEXT,
    last_error        TEXT,
    sort_order        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE projects (
    server_id   INTEGER NOT NULL REFERENCES servers(server_id) ON DELETE CASCADE,
    project_id  INTEGER NOT NULL,
    parent_id   INTEGER,
    name        TEXT    NOT NULL,
    identifier  TEXT,
    status      INTEGER NOT NULL DEFAULT 1,
    accessible  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (server_id, project_id)
);

CREATE TABLE project_settings (
    server_id      INTEGER NOT NULL REFERENCES servers(server_id) ON DELETE CASCADE,
    project_id     INTEGER NOT NULL,
    notify         INTEGER,
    watch_authored INTEGER,
    PRIMARY KEY (server_id, project_id)
);

CREATE TABLE issues (
    server_id          INTEGER NOT NULL REFERENCES servers(server_id) ON DELETE CASCADE,
    issue_id           INTEGER NOT NULL,
    project_id         INTEGER NOT NULL,
    tracker_id         INTEGER,
    subject            TEXT    NOT NULL DEFAULT '',
    description        TEXT,
    status_id          INTEGER,
    priority_id        INTEGER,
    assigned_to_id     INTEGER,
    assigned_to_name   TEXT,
    author_id          INTEGER,
    author_name        TEXT,
    created_on         TEXT,
    updated_on         TEXT,
    closed_on          TEXT,
    relation_flags     INTEGER NOT NULL DEFAULT 0,
    monitored          INTEGER NOT NULL DEFAULT 1,
    journals_synced_on TEXT,
    last_opened_at     TEXT,
    PRIMARY KEY (server_id, issue_id)
);
CREATE INDEX idx_issues_monitored ON issues (server_id, monitored, updated_on);
CREATE INDEX idx_issues_project ON issues (server_id, project_id);

CREATE TABLE journals (
    server_id     INTEGER NOT NULL REFERENCES servers(server_id) ON DELETE CASCADE,
    journal_id    INTEGER NOT NULL,
    issue_id      INTEGER NOT NULL,
    user_id       INTEGER,
    user_name     TEXT,
    notes         TEXT    NOT NULL DEFAULT '',
    private_notes INTEGER NOT NULL DEFAULT 0,
    created_on    TEXT,
    updated_on    TEXT,
    details_json  TEXT    NOT NULL DEFAULT '[]',
    PRIMARY KEY (server_id, journal_id)
);
CREATE INDEX idx_journals_issue ON journals (server_id, issue_id, journal_id);

CREATE TABLE attachments (
    server_id     INTEGER NOT NULL REFERENCES servers(server_id) ON DELETE CASCADE,
    attachment_id INTEGER NOT NULL,
    issue_id      INTEGER NOT NULL,
    filename      TEXT    NOT NULL,
    content_type  TEXT,
    filesize      INTEGER,
    content_url   TEXT,
    description   TEXT,
    author_name   TEXT,
    created_on    TEXT,
    local_path    TEXT,
    cached_at     TEXT,
    PRIMARY KEY (server_id, attachment_id)
);
CREATE INDEX idx_attachments_issue ON attachments (server_id, issue_id);

CREATE TABLE read_state (
    server_id            INTEGER NOT NULL REFERENCES servers(server_id) ON DELETE CASCADE,
    issue_id             INTEGER NOT NULL,
    last_read_journal_id INTEGER NOT NULL DEFAULT 0,
    read_at              TEXT,
    -- 初めて journal を取得したとき、この時刻(サーバー時刻)以前の journal を既読にする
    read_baseline        TEXT,
    PRIMARY KEY (server_id, issue_id)
);

CREATE TABLE drafts (
    server_id     INTEGER NOT NULL REFERENCES servers(server_id) ON DELETE CASCADE,
    issue_id      INTEGER NOT NULL,
    body          TEXT    NOT NULL DEFAULT '',
    private_notes INTEGER NOT NULL DEFAULT 0,
    failed_at     TEXT,
    error         TEXT,
    updated_at    TEXT,
    PRIMARY KEY (server_id, issue_id)
);

CREATE TABLE masters (
    server_id  INTEGER NOT NULL REFERENCES servers(server_id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL,
    id         INTEGER NOT NULL,
    name       TEXT    NOT NULL,
    extra_json TEXT    NOT NULL DEFAULT '{}',
    PRIMARY KEY (server_id, kind, id)
);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

MIGRATIONS: list[str] = [V1]

SCHEMA_VERSION = len(MIGRATIONS)
