"""プロジェクトとプロジェクト単位の設定(設計書 §3.2, §3.3)。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from remi.store.db import Database

PROJECT_STATUS_ACTIVE = 1
PROJECT_STATUS_CLOSED = 5
PROJECT_STATUS_ARCHIVED = 9

SETTING_KEYS = ("notify", "watch_authored", "watch_versions")
# ルートでの既定値(None = サーバー設定に従う)
ROOT_DEFAULTS: dict[str, bool | None] = {"notify": None, "watch_authored": False, "watch_versions": False}


@dataclass(frozen=True)
class Project:
    server_id: int
    project_id: int
    parent_id: int | None
    name: str
    identifier: str | None
    status: int
    accessible: bool

    @property
    def active(self) -> bool:
        return self.status == PROJECT_STATUS_ACTIVE


def _row(r) -> Project:
    return Project(r["server_id"], r["project_id"], r["parent_id"], r["name"], r["identifier"],
                   r["status"], bool(r["accessible"]))


class ProjectStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def replace_all(self, server_id: int, projects: list[dict[str, Any]]) -> None:
        """/projects.json の結果でプロジェクト一覧を置き換える。

        親が一覧にない(権限がない)場合は、parent の名前だけを使って accessible=0 で登録する。
        一覧から消えたプロジェクトは、チケットが残っていれば accessible=0 にし、なければ削除する。
        """
        seen: dict[int, tuple] = {}
        placeholders: dict[int, str] = {}
        for p in projects:
            pid = int(p["id"])
            parent = p.get("parent") or {}
            parent_id = int(parent["id"]) if parent.get("id") is not None else None
            seen[pid] = (pid, parent_id, p.get("name", ""), p.get("identifier"),
                         int(p.get("status", PROJECT_STATUS_ACTIVE)))
            if parent_id is not None:
                placeholders.setdefault(parent_id, parent.get("name", ""))
        with self.db.tx() as c:
            for pid, parent_id, name, ident, status in seen.values():
                c.execute(
                    "INSERT INTO projects(server_id, project_id, parent_id, name, identifier, status, accessible) "
                    "VALUES(?, ?, ?, ?, ?, ?, 1) ON CONFLICT(server_id, project_id) DO UPDATE SET "
                    "parent_id=excluded.parent_id, name=excluded.name, identifier=excluded.identifier, "
                    "status=excluded.status, accessible=1",
                    (server_id, pid, parent_id, name, ident, status),
                )
            for pid, name in placeholders.items():
                if pid in seen:
                    continue
                c.execute(
                    "INSERT INTO projects(server_id, project_id, parent_id, name, status, accessible) "
                    "VALUES(?, ?, NULL, ?, 1, 0) ON CONFLICT(server_id, project_id) DO UPDATE SET "
                    "name=excluded.name, accessible=0",
                    (server_id, pid, name),
                )
            keep = set(seen) | set(placeholders)
            for r in c.execute("SELECT project_id FROM projects WHERE server_id = ?", (server_id,)).fetchall():
                pid = r[0]
                if pid in keep:
                    continue
                used = c.execute(
                    "SELECT 1 FROM issues WHERE server_id = ? AND project_id = ? LIMIT 1", (server_id, pid)
                ).fetchone()
                if used:
                    c.execute("UPDATE projects SET accessible = 0 WHERE server_id = ? AND project_id = ?",
                              (server_id, pid))
                else:
                    c.execute("DELETE FROM projects WHERE server_id = ? AND project_id = ?", (server_id, pid))

    def ensure_placeholder(self, server_id: int, project_id: int, name: str) -> None:
        """チケットに含まれるプロジェクトが未登録なら、名前だけ登録しておく。"""
        with self.db.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO projects(server_id, project_id, parent_id, name, status, accessible) "
                "VALUES(?, ?, NULL, ?, 1, 0)",
                (server_id, project_id, name),
            )

    def list(self, server_id: int) -> list[Project]:
        rows = self.db.query("SELECT * FROM projects WHERE server_id = ? ORDER BY name COLLATE NOCASE",
                             (server_id,))
        return [_row(r) for r in rows]

    def get(self, server_id: int, project_id: int) -> Project | None:
        r = self.db.query_one("SELECT * FROM projects WHERE server_id = ? AND project_id = ?",
                              (server_id, project_id))
        return _row(r) if r else None

    def parent_map(self, server_id: int) -> dict[int, int | None]:
        return {r[0]: r[1] for r in self.db.query(
            "SELECT project_id, parent_id FROM projects WHERE server_id = ?", (server_id,))}

    def ancestors(self, server_id: int, project_id: int, parents: dict[int, int | None] | None = None) -> list[int]:
        """親から順に祖先のIDを返す(近い順)。循環していても止まる。"""
        parents = parents if parents is not None else self.parent_map(server_id)
        out: list[int] = []
        cur = parents.get(project_id)
        while cur is not None and cur not in out and cur != project_id:
            out.append(cur)
            cur = parents.get(cur)
        return out

    def descendants(self, server_id: int, project_id: int) -> set[int]:
        children: dict[int | None, list[int]] = {}
        for pid, parent in self.parent_map(server_id).items():
            children.setdefault(parent, []).append(pid)
        out: set[int] = set()
        stack = [project_id]
        while stack:
            for child in children.get(stack.pop(), []):
                if child not in out:
                    out.add(child)
                    stack.append(child)
        return out

    # --- プロジェクト単位の設定(3状態: None=継承 / True / False) -----------------
    def get_setting(self, server_id: int, project_id: int, key: str) -> bool | None:
        if key not in SETTING_KEYS:
            raise KeyError(key)
        r = self.db.query_one(
            f"SELECT {key} FROM project_settings WHERE server_id = ? AND project_id = ?",
            (server_id, project_id),
        )
        if r is None or r[0] is None:
            return None
        return bool(r[0])

    def set_setting(self, server_id: int, project_id: int, key: str, value: bool | None) -> None:
        if key not in SETTING_KEYS:
            raise KeyError(key)
        v = None if value is None else int(value)
        with self.db.tx() as c:
            c.execute(
                f"INSERT INTO project_settings(server_id, project_id, {key}) VALUES(?, ?, ?) "
                f"ON CONFLICT(server_id, project_id) DO UPDATE SET {key} = excluded.{key}",
                (server_id, project_id, v),
            )

    def effective_setting(self, server_id: int, project_id: int, key: str,
                          parents: dict[int, int | None] | None = None) -> bool | None:
        """自分 → 祖先の順にたどり、最初に見つかった オン/オフ を返す。なければルートの既定値。"""
        for pid in [project_id, *self.ancestors(server_id, project_id, parents)]:
            v = self.get_setting(server_id, pid, key)
            if v is not None:
                return v
        return ROOT_DEFAULTS[key]

    def projects_with_setting(self, server_id: int, key: str) -> set[int]:
        """実際の値がオンになっているプロジェクトのID。"""
        parents = self.parent_map(server_id)
        return {pid for pid in parents if self.effective_setting(server_id, pid, key, parents)}

    # --- 表示するチケット(関係のリスト。None=親から継承、最上位の既定はすべて) ------------------
    def get_visible_relations(self, server_id: int, project_id: int) -> list[str] | None:
        r = self.db.query_one(
            "SELECT visible_relations FROM project_settings WHERE server_id = ? AND project_id = ?",
            (server_id, project_id),
        )
        return json.loads(r[0]) if r is not None and r[0] is not None else None

    def set_visible_relations(self, server_id: int, project_id: int, value: list[str] | None) -> None:
        v = None if value is None else json.dumps(value)
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO project_settings(server_id, project_id, visible_relations) VALUES(?, ?, ?) "
                "ON CONFLICT(server_id, project_id) DO UPDATE SET visible_relations = excluded.visible_relations",
                (server_id, project_id, v),
            )

    def own_visible_relations(self, server_id: int) -> dict[int, list[str]]:
        """「表示するチケット」を自分で設定している(継承していない)プロジェクトと、その値。"""
        return {r[0]: json.loads(r[1]) for r in self.db.query(
            "SELECT project_id, visible_relations FROM project_settings "
            "WHERE server_id = ? AND visible_relations IS NOT NULL", (server_id,))}

    def visible_relations_map(self, server_id: int) -> dict[int, list[str]]:
        """プロジェクトごとの、継承を解決した「表示するチケット」。どこにも設定がない(すべて表示)プロジェクトは含めない。

        チケットのプロジェクトがプロジェクト一覧にない場合も、設定があれば含める。
        """
        own = self.own_visible_relations(server_id)
        if not own:
            return {}
        parents = self.parent_map(server_id)
        out: dict[int, list[str]] = {}
        for pid in set(parents) | set(own):
            for cur in [pid, *self.ancestors(server_id, pid, parents)]:
                if cur in own:
                    out[pid] = own[cur]
                    break
        return out
