"""プロジェクト一覧とマスタの取得(起動時と約1時間ごと。設計書 §3.2, §3.4)。"""

from __future__ import annotations

from remi.api.client import RedmineClient
from remi.store.db import Database
from remi.store.masters import MasterStore
from remi.store.projects import ProjectStore


def refresh_projects(client: RedmineClient, db: Database, server_id: int) -> int:
    projects = client.projects()
    ProjectStore(db).replace_all(server_id, projects)
    return len(projects)


def refresh_masters(client: RedmineClient, db: Database, server_id: int) -> None:
    ms = MasterStore(db)
    ms.replace(server_id, "status", client.issue_statuses(), ("is_closed",))
    ms.replace(server_id, "tracker", client.trackers())
    ms.replace(server_id, "priority", client.issue_priorities(), ("is_default",))


def refresh_metadata(client: RedmineClient, db: Database, server_id: int) -> None:
    refresh_masters(client, db, server_id)
    refresh_projects(client, db, server_id)
