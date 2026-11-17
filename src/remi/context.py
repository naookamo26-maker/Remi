"""アプリ全体で共有するオブジェクト(DB、各ストア、イベントバス)。"""

from __future__ import annotations

from dataclasses import dataclass, field

from remi.events import EventBus
from remi.store.db import Database
from remi.store.issues import IssueStore
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
