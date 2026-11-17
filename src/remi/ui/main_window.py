"""メインウィンドウ(3ペイン)。各ペインの中身は後のステップで実装する。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QSplitter, QWidget

from remi.ui.about import show_about


class MainWindow(QMainWindow):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Remi")
        self.resize(1280, 800)
        self.allow_close = False

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        for text in (self.tr("Servers and projects"), self.tr("Issues"), self.tr("Conversation")):
            label = QLabel(text, self.splitter)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setProperty("muted", True)
        self.splitter.setSizes([260, 340, 680])
        self.setCentralWidget(self.splitter)

        file_menu = self.menuBar().addMenu(self.tr("&File"))
        quit_action = QAction(self.tr("&Quit"), self)
        quit_action.triggered.connect(self.quit_app)
        file_menu.addAction(quit_action)

        help_menu = self.menuBar().addMenu(self.tr("&Help"))
        about = QAction(self.tr("&About Remi"), self)
        about.triggered.connect(lambda: show_about(self))
        help_menu.addAction(about)

    def bring_to_front(self) -> None:
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def quit_app(self) -> None:
        self.allow_close = True
        self.close()
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        # ×ボタンではトレイへ格納する(トレイはステップ8で実装)。終了はメニューから行う。
        if self.allow_close:
            event.accept()
        else:
            event.ignore()
            self.hide()
