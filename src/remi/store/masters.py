"""名前を引くためのマスタ(設計書 §3.4)。"""

from __future__ import annotations

import json
from typing import Any

from remi.store.db import Database

# version・category は、チケットやjournalに含まれていた名前と、個別に問い合わせた名前を覚える
KINDS = ("status", "tracker", "priority", "user", "custom_field", "version", "category")


class MasterStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def replace(self, server_id: int, kind: str, items: list[dict[str, Any]],
                extra_keys: tuple[str, ...] = (), with_position: bool = False) -> None:
        """with_position: Redmine が返した並び(ステータス・トラッカー・優先度は、管理画面の順序で返る)を、
        position(0から)として覚える。一覧の並べ替えを、名前順ではなく Redmine の順序にするのに使う。"""
        with self.db.tx() as c:
            c.execute("DELETE FROM masters WHERE server_id = ? AND kind = ?", (server_id, kind))
            for pos, it in enumerate(items):
                extra = {k: it.get(k) for k in extra_keys if k in it}
                if with_position:
                    extra["position"] = pos
                c.execute(
                    "INSERT INTO masters(server_id, kind, id, name, extra_json) VALUES(?, ?, ?, ?, ?)",
                    (server_id, kind, int(it["id"]), it.get("name", ""), json.dumps(extra)),
                )

    def merge(self, server_id: int, kind: str, items: list[dict[str, Any]], extra_keys: tuple[str, ...]) -> None:
        """名前と追加の情報を、ほかの行を消さずに書き込む(カスタムフィールドの定義など)。"""
        with self.db.tx() as c:
            for it in items:
                extra = {k: it.get(k) for k in extra_keys if k in it}
                c.execute(
                    "INSERT INTO masters(server_id, kind, id, name, extra_json) VALUES(?, ?, ?, ?, ?) "
                    "ON CONFLICT(server_id, kind, id) DO UPDATE SET name = excluded.name, extra_json = excluded.extra_json",
                    (server_id, kind, int(it["id"]), it.get("name", ""), json.dumps(extra)),
                )

    def extras(self, server_id: int, kind: str) -> dict[int, dict[str, Any]]:
        return {r[0]: json.loads(r[1] or "{}") for r in self.db.query(
            "SELECT id, extra_json FROM masters WHERE server_id = ? AND kind = ?", (server_id, kind))}

    def remember(self, server_id: int, kind: str, id_: int | None, name: str | None) -> None:
        """チケットやjournalに含まれていた名前を覚えておく(ユーザー・カスタムフィールド・バージョン・カテゴリ)。"""
        if id_ is None or not name:
            return
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO masters(server_id, kind, id, name) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(server_id, kind, id) DO UPDATE SET name = excluded.name",
                (server_id, kind, int(id_), name),
            )

    def names(self, server_id: int, kind: str) -> dict[int, str]:
        return {r[0]: r[1] for r in self.db.query(
            "SELECT id, name FROM masters WHERE server_id = ? AND kind = ?", (server_id, kind))}

    def closed_status_ids(self, server_id: int) -> set[int]:
        out = set()
        for r in self.db.query("SELECT id, extra_json FROM masters WHERE server_id = ? AND kind = 'status'",
                               (server_id,)):
            if json.loads(r[1] or "{}").get("is_closed"):
                out.add(r[0])
        return out
