"""QApplication の組み立てと起動。"""

from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from remi import APP_NAME, APP_USER_MODEL_ID, __version__
from remi.i18n import install_translators, resolve_language
from remi.logging_setup import setup_logging
from remi.paths import db_path
from remi.context import AppContext
from remi.store.db import SchemaTooNewError, open_database
from remi.sync.worker import SyncManager
from remi.system.single_instance import SingleInstance
from remi.ui.main_window import MainWindow
from remi.ui.theme import ThemeManager

log = logging.getLogger(__name__)


def _set_app_user_model_id() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:  # noqa: BLE001
        log.exception("failed to set AppUserModelID")


def run(argv: list[str]) -> int:
    setup_logging()
    _set_app_user_model_id()

    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)

    single = SingleInstance()
    if single.notify_existing():
        log.info("another instance is running; asked it to show itself")
        return 0
    single.listen()

    try:
        db = open_database(db_path())
    except SchemaTooNewError:
        QMessageBox.critical(
            None,
            APP_NAME,
            app.translate(
                "App",
                "The data was created by a newer version of Remi. Please update Remi.",
            ),
        )
        return 1
    ctx = AppContext(db)

    lang = resolve_language(ctx.settings.get("language"))
    app._remi_translators = install_translators(app, lang)  # type: ignore[attr-defined]
    ctx.theme = ThemeManager(app, ctx.settings.get("theme"))

    sync = SyncManager(db, app)
    sync.dataChanged.connect(ctx.signals.dataChanged)
    sync.stateChanged.connect(ctx.signals.syncStateChanged)
    ctx.signals.serversChanged.connect(sync.restart)
    ctx.signals.projectSettingsChanged.connect(lambda _sid: sync.sync_now())

    window = MainWindow(ctx, sync)
    single.activateRequested.connect(window.bring_to_front)
    window.show()
    sync.restart()
    if not ctx.servers.list():
        # 初回起動: サーバー登録を案内する
        from PySide6.QtCore import QTimer

        QTimer.singleShot(300, window.open_settings)

    code = app.exec()
    sync.stop_all()
    single.close()
    db.close_all()
    return code
