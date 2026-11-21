"""グローバル設定(settings テーブル)の読み書き。値はJSONで保存する。"""

from __future__ import annotations

import json
from typing import Any

from remi.store.db import Database

DEFAULTS: dict[str, Any] = {
    "theme": "system",  # system / light / dark(設計書 §9)
    "language": "system",  # system / ja / en(設計書 §10)
    "notify_enabled": True,
    "autostart": False,
    "show_closed": False,
    "version_show_closed": True,  # マイルストーン(バージョン)を開いたときの「クローズ済みも表示」。一覧とは別
    "unread_first": True,
    "list_density": "standard",  # 一覧の表示密度: standard / compact(設計書 §5.1)
    "allow_external_images": False,
    "image_cache_limit_mb": 500,
    "mine_side": "right",
    "window_geometry": None,
    "splitter_state": None,
    "tree_collapsed": [],
    "redmine_open_in": "tab",  # tab / app(別のウィンドウ) / browser(設計書 §5.4)。タブにできない環境では、別のウィンドウで開く
    "issue_details_open": False,  # 会話ビューのヘッダで「詳細」(ほかの項目)を開いているか
    "relation_filter": "all",  # 一覧の絞り込み: all / assigned / watching / authored
    # 「すべてのプロジェクトを表示」はサーバーごと(servers.show_all_projects)、
    # 「表示するチケット」はプロジェクトごと(project_settings.visible_relations)の設定
    "pane_sizes": [260, 360],
    "tray_hint_shown": False,  # 閉じる(×)でトレイへ格納することを、初めて格納したときに案内したか(画面からは変えない)
}


class Settings:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, key: str) -> Any:
        row = self.db.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        if row is None:
            return DEFAULTS.get(key)
        return json.loads(row["value"])

    def set(self, key: str, value: Any) -> None:
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )
