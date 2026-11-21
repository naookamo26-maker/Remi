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

# 「すべてのプロジェクトを表示」をサーバーごと、「表示するチケット」をサーバーごとの既定とプロジェクトごと
# (親から継承)の設定にする。それまでのグローバル設定は、すべてのサーバーに引き継ぐ
V2 = """
ALTER TABLE servers ADD COLUMN show_all_projects INTEGER NOT NULL DEFAULT 0;
ALTER TABLE servers ADD COLUMN visible_relations TEXT;
ALTER TABLE project_settings ADD COLUMN visible_relations TEXT;
UPDATE servers SET show_all_projects = 1
    WHERE (SELECT value FROM settings WHERE key = 'show_all_projects') = 'true';
UPDATE servers SET visible_relations = (SELECT value FROM settings WHERE key = 'visible_relations')
    WHERE json_array_length((SELECT value FROM settings WHERE key = 'visible_relations')) BETWEEN 1 AND 3;
DELETE FROM settings WHERE key IN ('show_all_projects', 'visible_relations')
"""

# ロードマップ(バージョン)の監視。プロジェクトごとに「未完了のバージョンをすべてウォッチ」(親から継承)し、
# バージョンに含まれるチケットの一覧と、その変化(追加・外れた・完了など)を記録する
V3 = """
ALTER TABLE project_settings ADD COLUMN watch_versions INTEGER;
CREATE TABLE versions (
    server_id      INTEGER NOT NULL REFERENCES servers(server_id) ON DELETE CASCADE,
    version_id     INTEGER NOT NULL,
    project_id     INTEGER NOT NULL,
    name           TEXT    NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'open',
    due_date       TEXT,
    description    TEXT,
    -- チケットの一覧を取得したか。最初の取得では変化を記録しない(そのときの状態を基準にする)
    issues_synced  INTEGER NOT NULL DEFAULT 0,
    -- ここまでの変化を見た(バージョンを選んで表示した)
    read_event_id  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (server_id, version_id)
);
CREATE TABLE version_issues (
    server_id  INTEGER NOT NULL REFERENCES servers(server_id) ON DELETE CASCADE,
    version_id INTEGER NOT NULL,
    issue_id   INTEGER NOT NULL,
    closed     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (server_id, version_id, issue_id)
);
CREATE TABLE version_events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id  INTEGER NOT NULL REFERENCES servers(server_id) ON DELETE CASCADE,
    version_id INTEGER NOT NULL,
    -- created / added / removed / closed / reopened / due_date / status / name
    kind       TEXT    NOT NULL,
    issue_id   INTEGER,
    subject    TEXT,
    old_value  TEXT,
    new_value  TEXT,
    created_at TEXT    NOT NULL
);
CREATE INDEX idx_version_events ON version_events (server_id, version_id, event_id)
"""

# サーバーの負荷を抑える(多数の利用者が同じRedmineを使う場合)。
# - バージョンのチケットの一覧は、件数と最新の更新日時(署名)が変わったときだけ取り直す
# - ポーリング間隔の「1分」を廃止し、3分にそろえる
V4 = """
ALTER TABLE versions ADD COLUMN issues_signature TEXT;
ALTER TABLE versions ADD COLUMN issues_full_at TEXT;
UPDATE servers SET poll_interval = 180 WHERE poll_interval < 180
"""

# 通知したコメントの記録。チケットのキャッシュを消して取り直したとき(監視対象から外れて戻った・削除の確認など)に、
# 同じコメントを再び通知しないよう、チケットのキャッシュとは別に残す(サーバーを削除したときだけ消える)
V5 = """
CREATE TABLE notified (
    server_id  INTEGER NOT NULL REFERENCES servers(server_id) ON DELETE CASCADE,
    issue_id   INTEGER NOT NULL,
    -- ここまでの journal は通知した(または通知の対象として扱った)
    journal_id INTEGER NOT NULL,
    PRIMARY KEY (server_id, issue_id)
)
"""

# 「作成したチケットを監視」(プロジェクトごとの設定 watch_authored)をやめ、「表示するチケット」の「自分が作成」に一本化する。
# 「自分が作成」を含むプロジェクトで、作成したチケットも取得する。既定は「自分が作成」を含まない(担当・グループ・ウォッチ)。
# これまでの設定を、見え方が変わらないように引き継ぐ:
# - 「作成したチケットを監視」をオンにしていたプロジェクトは、「自分が作成」を含める(これまでの既定は、すべてを含む)
# - ほかは、「自分が作成」を外す(これまでは取得していなかったので、表示するチケットに含めていても何も出ていなかった)
# watch_authored の列は、古いデータとの互換のため残す(使わない)
V6 = """
UPDATE project_settings
    SET visible_relations = json_insert(visible_relations, '$[#]', 'authored')
    WHERE watch_authored = 1 AND visible_relations IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM json_each(visible_relations) WHERE value = 'authored');
UPDATE project_settings
    SET visible_relations = '["assigned","group","watching","authored"]'
    WHERE watch_authored = 1 AND visible_relations IS NULL;
UPDATE project_settings
    SET visible_relations = (SELECT json_group_array(value) FROM json_each(visible_relations) WHERE value <> 'authored')
    WHERE COALESCE(watch_authored, 0) = 0 AND visible_relations IS NOT NULL
        AND EXISTS (SELECT 1 FROM json_each(visible_relations) WHERE value = 'authored');
UPDATE servers
    SET visible_relations = (SELECT json_group_array(value) FROM json_each(visible_relations) WHERE value <> 'authored')
    WHERE visible_relations IS NOT NULL
        AND EXISTS (SELECT 1 FROM json_each(visible_relations) WHERE value = 'authored')
"""

# V7: 初回同期でクローズ済みのチケットも取得するようにしたので、使い始めている利用者も、1回だけ初回同期をやり直す
# (キャッシュは消さない。既読・下書き・取得済みのコメントはそのまま残り、過去のクローズ済みだけが加わる)
V7 = """
UPDATE servers SET initial_sync_done = 0
"""

# V8: マイルストーンを表示するかの、サーバーごとの既定(プロジェクトの設定が「継承」のときに使う。初期値はオフ)。
# クローズ済みを残す日数の設定は廃止した(クローズ済みは「クローズ済みも表示」のときだけ見える)ので、保存していた値を消す
V8 = """
ALTER TABLE servers ADD COLUMN watch_versions INTEGER NOT NULL DEFAULT 0;
DELETE FROM settings WHERE key = 'closed_retention_days'
"""

# V9: 監視対象外になった時刻。関係が外れた未完了のチケットのキャッシュを、この時刻から一定期間(10日)経ってから削除する
# (更新日時から数えると、古いチケットは外れた直後に消えてしまうため)。すでに監視対象外のものは、更新した時点から数える
V9 = """
ALTER TABLE issues ADD COLUMN unmonitored_at TEXT;
UPDATE issues SET unmonitored_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE monitored = 0
"""

MIGRATIONS: list[str] = [V1, V2, V3, V4, V5, V6, V7, V8, V9]

SCHEMA_VERSION = len(MIGRATIONS)
