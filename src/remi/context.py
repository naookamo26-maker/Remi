"""アプリ全体で共有するオブジェクト(DB、各ストア、イベントバス)。"""

from __future__ import annotations

from dataclasses import dataclass, field

from remi.events import EventBus
from remi.store.db import Database
from remi.store.issues import RELATIONS, IssueStore, RelationScope
from remi.store.masters import MasterStore
from remi.store.projects import ProjectStore
from remi.store.servers import ServerStore
from remi.store.settings import Settings


@dataclass
class AppContext:
    db: Database
    bus: EventBus = field(default_factory=EventBus)
    settings: Settings = field(init=False)
    servers: ServerStore = field(init=False)
    projects: ProjectStore = field(init=False)
    masters: MasterStore = field(init=False)
    issues: IssueStore = field(init=False)

    def __post_init__(self) -> None:
        self.settings = Settings(self.db)
        self.servers = ServerStore(self.db)
        self.projects = ProjectStore(self.db)
        self.masters = MasterStore(self.db)
        self.issues = IssueStore(self.db)

    def relation_scope(self, relation_filter: str | None = None) -> RelationScope:
        """一覧に出すチケット(プロジェクトごとの「表示するチケット」と、一覧の絞り込みボタン)。

        relation_filter を省略すると、保存してある絞り込み(設定 relation_filter)を使う。
        """
        if relation_filter is None:
            relation_filter = self.settings.get("relation_filter")
        visible: dict[int, dict[int, list[str]]] = {}
        in_use: set[str] = set()
        for s in self.servers.list(enabled_only=True):
            m = self.projects.visible_relations_map(s.server_id)
            visible[s.server_id] = m
            for pid in self.issues.count_by_project(s.server_id):
                in_use.update(m.get(pid, RELATIONS))
        return RelationScope.build(visible, in_use, relation_filter)
