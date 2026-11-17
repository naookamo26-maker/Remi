"""メインウィンドウ(3ペイン。モック「メインウィンドウ」)。"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QEvent, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from remi.context import AppContext
from remi.ui.about import show_about
from remi.ui.tree import ProjectTree, Selection


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext, sync=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.sync = sync
        self.setWindowTitle("Remi")
        self.resize(1280, 800)
        self.allow_close = False
        self.minimize_to_tray = False  # トレイが使えるときに True にする

        # 左: ツリー + 設定・同期ボタン
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        self.tree = ProjectTree(ctx, left)
        left_layout.addWidget(self.tree, 1)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(8, 8, 8, 8)
        self.settings_button = QPushButton(self.tr("Settings"))
        self.settings_button.clicked.connect(self.open_settings)
        self.sync_button = QPushButton(self.tr("Sync now"))
        self.sync_button.clicked.connect(self.sync_now)
        buttons.addWidget(self.settings_button, 1)
        buttons.addWidget(self.sync_button)
        left_layout.addLayout(buttons)

        # 中央・右(ステップ5で実装)
        self.list_placeholder = QLabel(self.tr("Issues"))
        self.list_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.list_placeholder.setProperty("muted", True)
        self.conversation_placeholder = QLabel(self.tr("Select an issue to see the conversation."))
        self.conversation_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.conversation_placeholder.setProperty("muted", True)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(1)
        self.splitter.addWidget(left)
        self.splitter.addWidget(self.list_placeholder)
        self.splitter.addWidget(self.conversation_placeholder)
        self.splitter.setStretchFactor(2, 1)
        self.splitter.setSizes([260, 360, 660])
        self.setCentralWidget(self.splitter)

        self.status_label = QLabel()
        self.status_label.setProperty("muted", True)
        self.statusBar().addWidget(self.status_label, 1)

        self._build_menus()
        self.tree.selectionChangedTo.connect(self.on_selection)
        ctx.signals.syncStateChanged.connect(lambda _sid: self.update_status())
        ctx.signals.serversChanged.connect(self.update_status)
        self._restore_layout()
        self.update_status()

    # --- メニュー -------------------------------------------------------------
    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu(self.tr("&File"))
        settings = QAction(self.tr("&Settings..."), self)
        settings.setShortcut(QKeySequence.StandardKey.Preferences)
        settings.triggered.connect(self.open_settings)
        file_menu.addAction(settings)
        sync = QAction(self.tr("Sync &now"), self)
        sync.setShortcut(QKeySequence(Qt.Key.Key_F5))
        sync.triggered.connect(self.sync_now)
        file_menu.addAction(sync)
        file_menu.addSeparator()
        quit_action = QAction(self.tr("&Quit"), self)
        quit_action.setShortcut(QKeySequence(Qt.Modifier.CTRL | Qt.Key.Key_Q))
        quit_action.triggered.connect(self.quit_app)
        file_menu.addAction(quit_action)

        help_menu = self.menuBar().addMenu(self.tr("&Help"))
        about = QAction(self.tr("&About Remi"), self)
        about.triggered.connect(lambda: show_about(self))
        help_menu.addAction(about)

    # --- 動作 -----------------------------------------------------------------
    def open_settings(self) -> None:
        from remi.ui.settings_dialog import SettingsDialog

        SettingsDialog(self.ctx, self).exec()

    def sync_now(self) -> None:
        if self.sync is not None:
            self.sync.sync_now()

    def on_selection(self, selection: Selection) -> None:
        self.list_placeholder.setText(selection.title)

    def update_status(self) -> None:
        servers = self.ctx.servers.list(enabled_only=True)
        if not servers:
            self.status_label.setText(self.tr("No servers. Add a server in Settings."))
            return
        errors = [s.name for s in servers if s.last_error]
        times = [s.last_synced_at for s in servers if s.last_synced_at]
        parts = []
        if times:
            from remi.ui.format import format_time

            parts.append(self.tr("Last synced {time}").format(time=format_time(max(times))))
        if errors:
            parts.append(self.tr("Could not connect: {names}").format(names=", ".join(errors)))
        self.status_label.setText(" · ".join(parts))

    def bring_to_front(self) -> None:
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def quit_app(self) -> None:
        self.allow_close = True
        self.close()
        QApplication.quit()

    # --- 状態の保存 -------------------------------------------------------------
    def _restore_layout(self) -> None:
        geo = self.ctx.settings.get("window_geometry")
        if geo:
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
        state = self.ctx.settings.get("splitter_state")
        if state:
            self.splitter.restoreState(QByteArray.fromBase64(state.encode()))

    def _save_layout(self) -> None:
        self.ctx.settings.set("window_geometry", bytes(self.saveGeometry().toBase64().data()).decode())
        self.ctx.settings.set("splitter_state", bytes(self.splitter.saveState().toBase64().data()).decode())

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        if event.type() == QEvent.Type.ActivationChange and self.sync is not None:
            # 非アクティブの間はポーリング間隔を延ばす(設計書 §4.6)
            QTimer.singleShot(0, lambda: self.sync.set_active(self.isActiveWindow()))
        super().changeEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._save_layout()
        # ×ボタンではトレイへ格納する。終了はメニューから行う。
        if self.allow_close or not self.minimize_to_tray:
            event.accept()
            QApplication.quit()
        else:
            event.ignore()
            self.hide()
