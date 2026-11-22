"""サーバー設定の読み書き(servers テーブル + keyring)。"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field

from remi.paths import attachment_cache_dir
from remi.store import secrets
from remi.store.db import Database

POLL_INTERVALS = (180, 300)  # 秒(設定画面の選択肢)。多数の利用者でサーバーを叩きすぎないよう、1分は設けない


@dataclass
class Server:
    name: str
    url: str
    server_id: int | None = None
    enabled: bool = True
    poll_interval: int = 180
    notify_enabled: bool = True
    text_format: str = "common_mark"
    proxy_mode: str = "system"  # system / manual
    proxy_url: str | None = None
    my_user_id: int | None = None
    my_login: str | None = None
    my_name: str | None = None
    my_group_ids: list[int] = field(default_factory=list)
    last_sync_cursor: str | None = None
    initial_sync_done: bool = False
    last_synced_at: str | None = None
    last_error: str | None = None
    sort_order: int = 0
    show_all_projects: bool = False  # チケットのないプロジェクトもツリーに表示する
    # 表示するチケットの既定(プロジェクトで設定していなければこれを使う)。None はすべて
    visible_relations: list[str] | None = None
    # マイルストーンを表示するかの既定(プロジェクトの設定が「継承」のときに使う)
    watch_versions: bool = False

    def issue_url(self, issue_id: int) -> str:
        return f"{self.url}/issues/{issue_id}"

    @property
    def assignee_ids(self) -> set[int]:
        """自分が担当になりうるID(自分 + 所属グループ)。"""
        ids = set(self.my_group_ids)
        if self.my_user_id is not None:
            ids.add(self.my_user_id)
        return ids


_COLUMNS = (
    "name", "url", "enabled", "poll_interval", "notify_enabled", "text_format", "proxy_mode",
    "proxy_url", "my_user_id", "my_login", "my_name", "my_group_ids", "last_sync_cursor",
    "initial_sync_done", "last_synced_at", "last_error", "sort_order", "show_all_projects",
    "visible_relations", "watch_versions",
)


def _from_row(row) -> Server:
    return Server(
        server_id=row["server_id"],
        name=row["name"],
        url=row["url"],
        enabled=bool(row["enabled"]),
        poll_interval=row["poll_interval"],
        notify_enabled=bool(row["notify_enabled"]),
        text_format=row["text_format"],
        proxy_mode=row["proxy_mode"],
        proxy_url=row["proxy_url"],
        my_user_id=row["my_user_id"],
        my_login=row["my_login"],
        my_name=row["my_name"],
        my_group_ids=json.loads(row["my_group_ids"] or "[]"),
        last_sync_cursor=row["last_sync_cursor"],
        initial_sync_done=bool(row["initial_sync_done"]),
        last_synced_at=row["last_synced_at"],
        last_error=row["last_error"],
        sort_order=row["sort_order"],
        show_all_projects=bool(row["show_all_projects"]),
        visible_relations=json.loads(row["visible_relations"]) if row["visible_relations"] else None,
        watch_versions=bool(row["watch_versions"]),
    )


def _values(s: Server) -> list:
    return [
        s.name, s.url, int(s.enabled), s.poll_interval, int(s.notify_enabled), s.text_format,
        s.proxy_mode, s.proxy_url, s.my_user_id, s.my_login, s.my_name, json.dumps(s.my_group_ids),
        s.last_sync_cursor, int(s.initial_sync_done), s.last_synced_at, s.last_error, s.sort_order,
        int(s.show_all_projects), json.dumps(s.visible_relations) if s.visible_relations is not None else None,
        int(s.watch_versions),
    ]


class ServerStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def list(self, enabled_only: bool = False) -> list[Server]:
        sql = "SELECT * FROM servers"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY sort_order, server_id"
        return [_from_row(r) for r in self.db.query(sql)]

    def get(self, server_id: int) -> Server | None:
        row = self.db.query_one("SELECT * FROM servers WHERE server_id = ?", (server_id,))
        return _from_row(row) if row else None

    def save(self, server: Server, api_key: str | None = None) -> Server:
        with self.db.tx() as c:
            if server.server_id is None:
                cols = ", ".join(_COLUMNS)
                marks = ", ".join("?" for _ in _COLUMNS)
                cur = c.execute(f"INSERT INTO servers({cols}) VALUES({marks})", _values(server))
                server.server_id = cur.lastrowid
            else:
                sets = ", ".join(f"{col} = ?" for col in _COLUMNS)
                c.execute(f"UPDATE servers SET {sets} WHERE server_id = ?", [*_values(server), server.server_id])
        if api_key is not None:
            secrets.set_api_key(server.server_id, api_key)
        return server

    def update_fields(self, server_id: int, **fields) -> None:
        if not fields:
            return
        for k in fields:
            if k not in _COLUMNS:
                raise KeyError(k)
        sets = ", ".join(f"{k} = ?" for k in fields)
        values = [json.dumps(v) if k == "my_group_ids" or (k == "visible_relations" and v is not None)
                  else (int(v) if isinstance(v, bool) else v) for k, v in fields.items()]
        with self.db.tx() as c:
            c.execute(f"UPDATE servers SET {sets} WHERE server_id = ?", [*values, server_id])

    def reorder(self, server_ids: list[int]) -> None:
        """サーバーの並び順を、server_ids の順にする(0から番号を振り直す)。"""
        with self.db.tx() as c:
            c.executemany("UPDATE servers SET sort_order = ? WHERE server_id = ?",
                          [(i, sid) for i, sid in enumerate(server_ids)])

    def next_sort_order(self) -> int:
        """新しく登録するサーバーの並び順(いちばん最後)。削除で番号が空いても、既存のものと重ならないようにする。"""
        r = self.db.query_one("SELECT MAX(sort_order) FROM servers")
        return 0 if r is None or r[0] is None else int(r[0]) + 1

    def delete(self, server_id: int) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM servers WHERE server_id = ?", (server_id,))
        secrets.delete_api_key(server_id)
        shutil.rmtree(attachment_cache_dir(server_id), ignore_errors=True)

    def api_key(self, server_id: int) -> str | None:
        return secrets.get_api_key(server_id)
