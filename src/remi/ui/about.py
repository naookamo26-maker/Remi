"""「Remiについて」ダイアログ。"""

from __future__ import annotations

import platform

from PySide6.QtCore import QCoreApplication, qVersion
from PySide6.QtWidgets import QMessageBox, QWidget

from remi import APP_NAME, __version__


def show_about(parent: QWidget | None) -> None:
    QMessageBox.about(
        parent,
        QCoreApplication.translate("About", "About Remi"),
        QCoreApplication.translate(
            "About",
            "<h3>{name} {version}</h3>"
            "<p>A desktop app to follow and comment on Redmine issues like a chat.</p>"
            "<p>Python {py} / Qt {qt}</p>",
        ).format(name=APP_NAME, version=__version__, py=platform.python_version(), qt=qVersion()),
    )
