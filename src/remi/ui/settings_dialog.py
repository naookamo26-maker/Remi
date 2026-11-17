"""設定ダイアログ(全般・サーバー)。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from remi.context import AppContext
from remi.i18n import SUPPORTED_LANGUAGES
from remi.ui.server_dialog import ServerDialog


class SettingsDialog(QDialog):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle(self.tr("Settings"))
        self.setMinimumSize(560, 420)
        tabs = QTabWidget(self)
        tabs.addTab(self._general_tab(), self.tr("General"))
        tabs.addTab(self._servers_tab(), self.tr("Servers"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    # --- 全般 -----------------------------------------------------------------
    def _general_tab(self) -> QWidget:
        s = self.ctx.settings
        w = QWidget()
        form = QFormLayout(w)

        self.language_combo = QComboBox()
        self.language_combo.addItem(self.tr("Follow system"), "system")
        for code, name in SUPPORTED_LANGUAGES.items():
            self.language_combo.addItem(name, code)
        self.language_combo.setCurrentIndex(max(0, self.language_combo.findData(s.get("language"))))
        self.language_note = QLabel(self.tr("Changes take effect after restarting Remi."))
        self.language_note.setProperty("muted", True)
        self.language_note.setVisible(False)
        self.language_combo.currentIndexChanged.connect(self._language_changed)
        lang_box = QVBoxLayout()
        lang_box.addWidget(self.language_combo)
        lang_box.addWidget(self.language_note)
        form.addRow(self.tr("Language"), lang_box)

        self.theme_combo = QComboBox()
        for value, label in (("system", self.tr("Follow system")), ("light", self.tr("Light")),
                             ("dark", self.tr("Dark"))):
            self.theme_combo.addItem(label, value)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(s.get("theme"))))
        self.theme_combo.currentIndexChanged.connect(self._theme_changed)
        form.addRow(self.tr("Appearance"), self.theme_combo)

        self.mine_combo = QComboBox()
        self.mine_combo.addItem(self.tr("Right"), "right")
        self.mine_combo.addItem(self.tr("Left"), "left")
        self.mine_combo.setCurrentIndex(max(0, self.mine_combo.findData(s.get("mine_side"))))
        self.mine_combo.currentIndexChanged.connect(
            lambda: self._set("mine_side", self.mine_combo.currentData()))
        form.addRow(self.tr("My comments"), self.mine_combo)

        self.notify_check = QCheckBox(self.tr("Show desktop notifications"))
        self.notify_check.setChecked(bool(s.get("notify_enabled")))
        self.notify_check.toggled.connect(lambda v: self._set("notify_enabled", v))
        form.addRow(self.tr("Notifications"), self.notify_check)

        self.autostart_check = QCheckBox(self.tr("Start Remi when I sign in to Windows"))
        self.autostart_check.setChecked(bool(s.get("autostart")))
        self.autostart_check.toggled.connect(lambda v: self._set("autostart", v))
        form.addRow(self.tr("Startup"), self.autostart_check)

        self.retention_spin = QSpinBox()
        self.retention_spin.setRange(0, 365)
        self.retention_spin.setSuffix(self.tr(" day(s)"))
        self.retention_spin.setValue(int(s.get("closed_retention_days")))
        self.retention_spin.valueChanged.connect(lambda v: self._set("closed_retention_days", v))
        form.addRow(self.tr("Keep closed issues for"), self.retention_spin)

        self.external_images_check = QCheckBox(self.tr("Load images from external URLs"))
        self.external_images_check.setChecked(bool(s.get("allow_external_images")))
        self.external_images_check.toggled.connect(lambda v: self._set("allow_external_images", v))
        form.addRow(self.tr("Images"), self.external_images_check)
        return w

    def _set(self, key: str, value) -> None:
        self.ctx.settings.set(key, value)
        self.ctx.signals.settingsChanged.emit(key)

    def _language_changed(self) -> None:
        self._set("language", self.language_combo.currentData())
        self.language_note.setVisible(True)

    def _theme_changed(self) -> None:
        mode = self.theme_combo.currentData()
        self._set("theme", mode)
        if self.ctx.theme is not None:
            self.ctx.theme.set_mode(mode)

    # --- サーバー -------------------------------------------------------------
    def _servers_tab(self) -> QWidget:
        w = QWidget()
        self.server_list = QListWidget()
        self.server_list.itemDoubleClicked.connect(lambda _i: self.edit_server())
        add = QPushButton(self.tr("Add..."))
        add.clicked.connect(self.add_server)
        self.edit_button = QPushButton(self.tr("Edit..."))
        self.edit_button.clicked.connect(self.edit_server)
        self.delete_button = QPushButton(self.tr("Delete"))
        self.delete_button.clicked.connect(self.delete_server)
        side = QVBoxLayout()
        for b in (add, self.edit_button, self.delete_button):
            side.addWidget(b)
        side.addStretch(1)
        row = QHBoxLayout(w)
        row.addWidget(self.server_list, 1)
        row.addLayout(side)
        self.server_list.currentRowChanged.connect(lambda _r: self._update_server_buttons())
        self.reload_servers()
        return w

    def reload_servers(self) -> None:
        self.server_list.clear()
        for s in self.ctx.servers.list():
            label = s.name if s.enabled else self.tr("{name} (disabled)").format(name=s.name)
            item = QListWidgetItem(f"{label}\n{s.url}")
            item.setData(Qt.ItemDataRole.UserRole, s.server_id)
            self.server_list.addItem(item)
        self._update_server_buttons()

    def _update_server_buttons(self) -> None:
        has = self.server_list.currentItem() is not None
        self.edit_button.setEnabled(has)
        self.delete_button.setEnabled(has)

    def _current_server_id(self) -> int | None:
        item = self.server_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def add_server(self) -> None:
        dlg = ServerDialog(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            server, key = dlg.build_server()
            server.sort_order = len(self.ctx.servers.list())
            self.ctx.servers.save(server, api_key=key)
            self.reload_servers()
            self.ctx.signals.serversChanged.emit()

    def edit_server(self) -> None:
        sid = self._current_server_id()
        if sid is None:
            return
        server = self.ctx.servers.get(sid)
        dlg = ServerDialog(server, self.ctx.servers.api_key(sid), parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            server, key = dlg.build_server()
            self.ctx.servers.save(server, api_key=key)
            self.reload_servers()
            self.ctx.signals.serversChanged.emit()

    def delete_server(self) -> None:
        sid = self._current_server_id()
        if sid is None:
            return
        server = self.ctx.servers.get(sid)
        answer = QMessageBox.question(
            self, self.tr("Delete server"),
            self.tr("Delete \"{name}\"? Cached issues and comments for this server are also deleted. "
                    "Nothing is deleted on the Redmine server.").format(name=server.name),
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.ctx.servers.delete(sid)
            self.reload_servers()
            self.ctx.signals.serversChanged.emit()
