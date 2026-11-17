"""アプリ全体で共有するオブジェクト(DB、各ストア、テーマなど)。"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Signal

from remi.store.db import Database
from remi.store.issues import IssueStore
from remi.store.masters import MasterStore
from remi.store.projects import ProjectStore
from remi.store.servers import ServerStore
from remi.store.settings import Settings


class AppSignals(QObject):
    serversChanged = Signal()  # サーバーの追加・編集・削除
    projectSettingsChanged = Signal(int)  # server_id
    settingsChanged = Signal(str)  # 変更されたグローバル設定のキー
    dataChanged = Signal(int, object)  # server_id, 変更された issue_id の集合(None = 全体)
    syncStateChanged = Signal(int)  # server_id(接続状態・最終同期時刻の変化)


@dataclass
class AppContext:
    db: Database
    settings: Settings = field(init=False)
    servers: ServerStore = field(init=False)
    projects: ProjectStore = field(init=False)
    masters: MasterStore = field(init=False)
    issues: IssueStore = field(init=False)
    signals: AppSignals = field(init=False)
    theme: object | None = None  # ThemeManager(テストでは None のこともある)

    def __post_init__(self) -> None:
        self.settings = Settings(self.db)
        self.servers = ServerStore(self.db)
        self.projects = ProjectStore(self.db)
        self.masters = MasterStore(self.db)
        self.issues = IssueStore(self.db)
        self.signals = AppSignals()
