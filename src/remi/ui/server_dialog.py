"""サーバーの登録・編集ダイアログ(設計書 §3.1, モック「サーバー登録ダイアログ」)。"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from remi.api.client import RedmineClient, normalize_url
from remi.api.errors import RedmineError
from remi.api.probe import ProbeResult, probe_server
from remi.store.servers import POLL_INTERVALS, Server
from remi.ui.errors import error_message
from remi.ui.worker import run_in_background

log = logging.getLogger(__name__)


class ServerDialog(QDialog):
    """新規登録では接続テストに成功するまで「登録」を押せない。"""

    def __init__(self, server: Server | None = None, api_key: str | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.server = server
        self.result_probe: ProbeResult | None = None
        self._tested_with: tuple[str, str] | None = None
        self.setWindowTitle(self.tr("Edit server") if server else self.tr("Add server"))
        self.setMinimumWidth(620)

        self.name_edit = QLineEdit()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://redmine.example.com")
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        key_help = QLabel(self.tr("Find it in Redmine under My account > API access key. "
                                  "It is stored in Windows Credential Manager."))
        key_help.setWordWrap(True)
        key_help.setProperty("muted", True)

        form = QFormLayout()
        form.addRow(self.tr("Display name"), self.name_edit)
        form.addRow(self.tr("URL"), self.url_edit)
        key_box = QVBoxLayout()
        key_box.addWidget(self.key_edit)
        key_box.addWidget(key_help)
        form.addRow(self.tr("API access key"), key_box)

        self.test_button = QPushButton(self.tr("Test connection"))
        self.test_button.clicked.connect(self.run_test)
        self.test_label = QLabel()
        self.test_label.setWordWrap(True)
        self.test_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        test_row = QHBoxLayout()
        test_row.addWidget(self.test_button, 0, Qt.AlignmentFlag.AlignTop)
        test_row.addWidget(self.test_label, 1)

        # テキスト書式
        fmt_box = QGroupBox(self.tr("Text formatting"))
        fmt_layout = QVBoxLayout(fmt_box)
        self.guess_label = QLabel(self.tr("Choose the same setting as Administration > Settings > General > "
                                          "Text formatting in Redmine."))
        self.guess_label.setWordWrap(True)
        fmt_layout.addWidget(self.guess_label)
        self.fmt_group = QButtonGroup(self)
        self.fmt_buttons: dict[str, QRadioButton] = {}
        for value, label in (
            ("common_mark", self.tr("Markdown (CommonMark / GitHub flavored)")),
            ("textile", self.tr("Textile")),
            ("plain", self.tr("None (plain text)")),
        ):
            rb = QRadioButton(label)
            self.fmt_group.addButton(rb)
            self.fmt_buttons[value] = rb
            fmt_layout.addWidget(rb)

        # 同期と通知
        sync_box = QGroupBox(self.tr("Sync and notifications"))
        sync_form = QFormLayout(sync_box)
        self.poll_combo = QComboBox()
        for sec in POLL_INTERVALS:
            self.poll_combo.addItem(self.tr("%n minute(s)", "", sec // 60), sec)
        sync_form.addRow(self.tr("Check for updates every"), self.poll_combo)
        self.notify_check = QCheckBox(self.tr("Notify me of new comments on this server"))
        sync_form.addRow(self.tr("Notifications"), self.notify_check)
        self.proxy_system = QRadioButton(self.tr("Use Windows settings"))
        self.proxy_manual = QRadioButton(self.tr("Specify manually"))
        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText("http://proxy.example.com:8080")
        proxy_row = QHBoxLayout()
        proxy_row.addWidget(self.proxy_system)
        proxy_row.addWidget(self.proxy_manual)
        proxy_row.addWidget(self.proxy_edit, 1)
        sync_form.addRow(self.tr("Proxy"), proxy_row)
        self.proxy_manual.toggled.connect(self.proxy_edit.setEnabled)

        self.enabled_check = QCheckBox(self.tr("Enable this server"))

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.save_button = self.buttons.addButton(
            self.tr("Save") if server else self.tr("Add"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.save_button.setProperty("primary", True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(test_row)
        layout.addWidget(fmt_box)
        layout.addWidget(sync_box)
        layout.addWidget(self.enabled_check)
        layout.addWidget(self.buttons)

        self._load(server, api_key)
        for w in (self.url_edit, self.key_edit):
            w.textChanged.connect(self._update_buttons)
        self.name_edit.textChanged.connect(self._update_buttons)
        self._update_buttons()

    # --- 表示 ---------------------------------------------------------------
    def _load(self, s: Server | None, api_key: str | None) -> None:
        s = s or Server(name="", url="")
        self.name_edit.setText(s.name)
        self.url_edit.setText(s.url)
        self.key_edit.setText(api_key or "")
        self.fmt_buttons.get(s.text_format, self.fmt_buttons["common_mark"]).setChecked(True)
        idx = self.poll_combo.findData(s.poll_interval)
        self.poll_combo.setCurrentIndex(idx if idx >= 0 else 1)
        self.notify_check.setChecked(s.notify_enabled)
        (self.proxy_manual if s.proxy_mode == "manual" else self.proxy_system).setChecked(True)
        self.proxy_edit.setText(s.proxy_url or "")
        self.proxy_edit.setEnabled(s.proxy_mode == "manual")
        self.enabled_check.setChecked(s.enabled)
        if self.server is not None:
            self._tested_with = (s.url, api_key or "")

    def _connection_inputs(self) -> tuple[str, str]:
        return self.url_edit.text().strip(), self.key_edit.text().strip()

    def _update_buttons(self) -> None:
        url, key = self._connection_inputs()
        self.test_button.setEnabled(bool(url and key))
        tested = self._tested_with is not None and self._tested_with == self._normalized_inputs()
        self.save_button.setEnabled(bool(self.name_edit.text().strip()) and tested)

    def _normalized_inputs(self) -> tuple[str, str] | None:
        url, key = self._connection_inputs()
        try:
            return normalize_url(url), key
        except RedmineError:
            return None

    def _set_status(self, text: str, kind: str) -> None:
        self.test_label.setText(text)
        for k in ("error", "success", "muted"):
            self.test_label.setProperty(k, k == kind)
        self.test_label.style().unpolish(self.test_label)
        self.test_label.style().polish(self.test_label)

    # --- 接続テスト ----------------------------------------------------------
    def make_client(self) -> RedmineClient:
        url, key = self._connection_inputs()
        return RedmineClient(
            url, key,
            proxy_mode="manual" if self.proxy_manual.isChecked() else "system",
            proxy_url=self.proxy_edit.text().strip() or None,
            min_interval=0.2,
        )

    def run_test(self) -> None:
        try:
            client = self.make_client()
        except RedmineError as e:
            self._set_status(error_message(e), "error")
            return
        self.test_button.setEnabled(False)
        self._set_status(self.tr("Connecting..."), "muted")
        inputs = self._normalized_inputs()

        def done(result: ProbeResult) -> None:
            client.close()
            self.result_probe = result
            self._tested_with = inputs
            self._set_status(
                self.tr("Connected. User: {name} (ID {id}) · {groups} group(s) · "
                        "assigned {assigned} / watching {watched} open issue(s)").format(
                    name=result.name, id=result.user_id, groups=len(result.group_ids),
                    assigned=result.assigned_count, watched=result.watched_count),
                "success",
            )
            self._show_guess(result)
            if not self.name_edit.text().strip():
                self.name_edit.setText(client.base_url.split("://", 1)[-1])
            self._update_buttons()

        def failed(err: Exception) -> None:
            client.close()
            msg = error_message(err) if isinstance(err, RedmineError) else str(err)
            log.warning("connection test failed: %s", err)
            self._set_status(msg, "error")
            self._update_buttons()

        self._task = run_in_background(lambda: probe_server(client), done, failed, self)

    def _show_guess(self, result: ProbeResult) -> None:
        g = result.format_guess
        if g is None or g.text_format is None:
            self.guess_label.setText(self.tr("Could not guess the text formatting from recent comments. "
                                             "Choose the same setting as in Redmine."))
            return
        name = {"common_mark": self.tr("Markdown"), "textile": self.tr("Textile")}[g.text_format]
        self.guess_label.setText(self.tr("Guessed <b>{name}</b> from {n} recent text(s). Choose the same "
                                         "setting as Administration > Settings > General > Text formatting "
                                         "in Redmine.").format(name=name, n=g.samples))
        if self.server is None:
            self.fmt_buttons[g.text_format].setChecked(True)

    # --- 結果 ---------------------------------------------------------------
    def text_format(self) -> str:
        return next(k for k, b in self.fmt_buttons.items() if b.isChecked())

    def build_server(self) -> tuple[Server, str]:
        """入力内容から Server を作る(server_id は編集時のみ引き継ぐ)。"""
        url, key = self._connection_inputs()
        base = self.server or Server(name="", url="")
        base.name = self.name_edit.text().strip()
        base.url = normalize_url(url)
        base.text_format = self.text_format()
        base.poll_interval = int(self.poll_combo.currentData())
        base.notify_enabled = self.notify_check.isChecked()
        base.proxy_mode = "manual" if self.proxy_manual.isChecked() else "system"
        base.proxy_url = self.proxy_edit.text().strip() or None
        base.enabled = self.enabled_check.isChecked()
        if self.result_probe is not None:
            base.my_user_id = self.result_probe.user_id
            base.my_login = self.result_probe.login
            base.my_name = self.result_probe.name
            base.my_group_ids = self.result_probe.group_ids
        return base, key
