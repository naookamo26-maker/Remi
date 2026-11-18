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
    "show_all_projects": False,
    "show_closed": False,
    "closed_retention_days": 7,
    "unread_first": True,
    "allow_external_images": False,
    "image_cache_limit_mb": 500,
    "mine_side": "right",
    "window_geometry": None,
    "splitter_state": None,
    "tree_collapsed": [],
    "redmine_open_in": "app",  # app / browser(設計書 §5.4)
    "relation_filter": "all",  # 一覧の絞り込み: all / assigned / watching / authored
    # 表示するチケット(ツリーの件数・未読数と一覧に反映する): assigned / group / watching / authored
    "visible_relations": ["assigned", "group", "watching", "authored"],
    "pane_sizes": [260, 360],
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
