"""ロードマップ(バージョン)の監視: バージョン・含まれるチケット・変化の記録(設計書 §5.5)。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from remi.store.db import Database

# 変化の種類
EVENT_KINDS = ("created", "added", "removed", "closed", "reopened", "due_date", "status", "name")


@dataclass(frozen=True)
class Version:
    server_id: int
    version_id: int
    project_id: int
    name: str
    status: str  # open / locked / closed
    due_date: str | None
    description: str | None
    issues_synced: bool
    read_event_id: int
    issues_signature: str | None = None  # 前回取り直したときのチケットの一覧の署名(件数と最新の更新日時)
    issues_full_at: str | None = None  # 前回チケットの一覧を全件取り直した時刻(UTC)

    @property
    def closed(self) -> bool:
        return self.status == "closed"


@dataclass(frozen=True)
class VersionEvent:
    event_id: int
    version_id: int
    kind: str
    issue_id: int | None
    subject: str | None
    old_value: str | None
    new_value: str | None
    created_at: str


def _version(r) -> Version:
    return Version(r["server_id"], r["version_id"], r["project_id"], r["name"], r["status"], r["due_date"],
                   r["description"], bool(r["issues_synced"]), r["read_event_id"],
                   r["issues_signature"], r["issues_full_at"])


class VersionStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # --- バージョン ---------------------------------------------------------------
    def list(self, server_id: int) -> list[Version]:
        return [_version(r) for r in self.db.query(
            "SELECT * FROM versions WHERE server_id = ? ORDER BY COALESCE(due_date, '9999'), name COLLATE NOCASE",
            (server_id,))]

    def get(self, server_id: int, version_id: int) -> Version | None:
        r = self.db.query_one("SELECT * FROM versions WHERE server_id = ? AND version_id = ?",
                              (server_id, version_id))
        return _version(r) if r else None

    def upsert(self, server_id: int, data: dict[str, Any]) -> None:
        """REST APIのバージョン(JSON)を保存する。"""
        with self.db.tx() as c:
            c.execute(
                """INSERT INTO versions(server_id, version_id, project_id, name, status, due_date, description)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(server_id, version_id) DO UPDATE SET
                   project_id=excluded.project_id, name=excluded.name, status=excluded.status,
                   due_date=excluded.due_date, description=excluded.description""",
                (server_id, int(data["id"]), int(data["project"]["id"]), data.get("name", ""),
                 data.get("status") or "open", data.get("due_date") or data.get("effective_date"),
                 data.get("description")),
            )

    def delete(self, server_id: int, version_ids: list[int] | set[int]) -> None:
        with self.db.tx() as c:
            for vid in version_ids:
                for table in ("versions", "version_issues", "version_events"):
                    c.execute(f"DELETE FROM {table} WHERE server_id = ? AND version_id = ?", (server_id, vid))

    # --- 含まれるチケット ------------------------------------------------------------
    def members(self, server_id: int, version_id: int) -> dict[int, bool]:
        """issue_id → 完了しているか。"""
        return {r[0]: bool(r[1]) for r in self.db.query(
            "SELECT issue_id, closed FROM version_issues WHERE server_id = ? AND version_id = ?",
            (server_id, version_id))}

    def set_members(self, server_id: int, version_id: int, members: dict[int, bool]) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM version_issues WHERE server_id = ? AND version_id = ?", (server_id, version_id))
            c.executemany("INSERT INTO version_issues(server_id, version_id, issue_id, closed) VALUES(?,?,?,?)",
                          [(server_id, version_id, iid, int(closed)) for iid, closed in members.items()])
            c.execute("UPDATE versions SET issues_synced = 1 WHERE server_id = ? AND version_id = ?",
                      (server_id, version_id))

    def set_issues_signature(self, server_id: int, version_id: int, signature: str | None,
                             full_at: str | None = None) -> None:
        """チケットの一覧の署名を記録する。full_at を渡したときは、全件を取り直した時刻も記録する。"""
        with self.db.tx() as c:
            if full_at is None:
                c.execute("UPDATE versions SET issues_signature = ? WHERE server_id = ? AND version_id = ?",
                          (signature, server_id, version_id))
            else:
                c.execute("UPDATE versions SET issues_signature = ?, issues_full_at = ? "
                          "WHERE server_id = ? AND version_id = ?", (signature, full_at, server_id, version_id))

    def progress(self, server_id: int) -> dict[int, tuple[int, int]]:
        """version_id → (完了したチケットの数, 全体の数)。"""
        return {r[0]: (int(r[1] or 0), r[2]) for r in self.db.query(
            "SELECT version_id, SUM(closed), COUNT(*) FROM version_issues WHERE server_id = ? GROUP BY version_id",
            (server_id,))}

    def all_issue_ids(self) -> set[tuple[int, int]]:
        """いずれかのバージョンに含まれるチケット (server_id, issue_id)。"""
        return {(r[0], r[1]) for r in self.db.query("SELECT DISTINCT server_id, issue_id FROM version_issues")}

    # --- 変化の記録 ---------------------------------------------------------------
    def add_event(self, server_id: int, version_id: int, kind: str, created_at: str, *, issue_id: int | None = None,
                  subject: str | None = None, old_value: str | None = None, new_value: str | None = None) -> None:
        if kind not in EVENT_KINDS:
            raise ValueError(kind)
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO version_events(server_id, version_id, kind, issue_id, subject, old_value, new_value, "
                "created_at) VALUES(?,?,?,?,?,?,?,?)",
                (server_id, version_id, kind, issue_id, subject, old_value, new_value, created_at),
            )

    def events(self, server_id: int, version_id: int, limit: int = 300) -> list[VersionEvent]:
        """新しいものから limit 件を、古い順に返す。"""
        rows = self.db.query(
            "SELECT * FROM version_events WHERE server_id = ? AND version_id = ? ORDER BY event_id DESC LIMIT ?",
            (server_id, version_id, limit))
        return [VersionEvent(r["event_id"], r["version_id"], r["kind"], r["issue_id"], r["subject"],
                             r["old_value"], r["new_value"], r["created_at"]) for r in reversed(rows)]

    def unread_counts(self, server_id: int) -> dict[int, int]:
        """version_id → まだ見ていない変化の数。"""
        return {r[0]: r[1] for r in self.db.query(
            """SELECT e.version_id, COUNT(*) FROM version_events e
               JOIN versions v ON v.server_id = e.server_id AND v.version_id = e.version_id
               WHERE e.server_id = ? AND e.event_id > v.read_event_id GROUP BY e.version_id""",
            (server_id,))}

    def mark_read(self, server_id: int, version_id: int) -> None:
        with self.db.tx() as c:
            c.execute(
                """UPDATE versions SET read_event_id = COALESCE((SELECT MAX(event_id) FROM version_events
                       WHERE server_id = ? AND version_id = ?), 0)
                   WHERE server_id = ? AND version_id = ?""",
                (server_id, version_id, server_id, version_id))
