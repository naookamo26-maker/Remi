"""テーマ(ライト / ダーク)とセマンティックカラートークン(設計書 §9)。

色はここで定義したトークン経由でだけ使う。ウィジェット(QPalette + スタイルシート)、
会話ビューのHTML(CSS)、コードハイライト(Pygments)のすべてがこのトークンから作られる。
"""

from __future__ import annotations

import sys
from dataclasses import asdict, dataclass

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

THEME_MODES = ("system", "light", "dark")


@dataclass(frozen=True)
class Palette:
    name: str
    window: str
    surface: str
    surface_alt: str
    selection: str
    text: str
    text_muted: str
    border: str
    accent: str
    on_accent: str
    link: str
    bubble_other: str
    bubble_other_border: str
    bubble_mine: str
    bubble_private: str
    bubble_private_border: str
    code_bg: str
    code_border: str
    mention_bg: str
    mention_fg: str
    danger: str
    success: str
    warning_bg: str
    warning_fg: str
    pygments_style: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)

    @property
    def is_dark(self) -> bool:
        return self.name == "dark"


LIGHT = Palette(
    name="light",
    window="#FFFFFF",
    surface="#F7F8FA",
    surface_alt="#FAFBFC",
    selection="#E3EDF8",
    text="#1F2328",
    text_muted="#5B6270",
    border="#DDE1E6",
    accent="#0F6CBD",
    on_accent="#FFFFFF",
    link="#0F6CBD",
    bubble_other="#FFFFFF",
    bubble_other_border="#E1E4E8",
    bubble_mine="#DCEBFA",
    bubble_private="#FFFDF0",
    bubble_private_border="#EADFA8",
    code_bg="#F6F8FA",
    code_border="#E1E4E8",
    mention_bg="#C3DBF5",
    mention_fg="#0B4F8A",
    danger="#A40E26",
    success="#116329",
    warning_bg="#FFF8C5",
    warning_fg="#6F4E00",
    pygments_style="default",
)

DARK = Palette(
    name="dark",
    window="#1E1F22",
    surface="#26282C",
    surface_alt="#1A1B1E",
    selection="#203A57",
    text="#E6E8EB",
    text_muted="#A3A9B3",
    border="#3A3D42",
    accent="#62A8F0",
    on_accent="#0B1620",
    link="#6CB4FF",
    bubble_other="#2B2D31",
    bubble_other_border="#3A3D42",
    bubble_mine="#1F3A5A",
    bubble_private="#332D1A",
    bubble_private_border="#5C5130",
    code_bg="#16171A",
    code_border="#3A3D42",
    mention_bg="#23466E",
    mention_fg="#CFE6FF",
    danger="#FF8A80",
    success="#6FDD8B",
    warning_bg="#3D3000",
    warning_fg="#F2CC60",
    pygments_style="github-dark",
)


def contrast_ratio(fg: str, bg: str) -> float:
    """WCAG 2.x のコントラスト比。"""

    def lum(hex_: str) -> float:
        c = QColor(hex_)
        chans = []
        for v in (c.redF(), c.greenF(), c.blueF()):
            chans.append(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4)
        r, g, b = chans
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    a, b = lum(fg), lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# 文字色と背景色の組み合わせ(テストで 4.5:1 以上を確認する)
CONTRAST_PAIRS: list[tuple[str, str]] = [
    ("text", "window"),
    ("text", "surface"),
    ("text", "surface_alt"),
    ("text", "selection"),
    ("text_muted", "window"),
    ("text_muted", "surface"),
    ("text_muted", "surface_alt"),
    ("on_accent", "accent"),
    ("link", "window"),
    ("link", "surface_alt"),
    ("text", "bubble_other"),
    ("text", "bubble_mine"),
    ("text", "bubble_private"),
    ("text", "code_bg"),
    ("mention_fg", "mention_bg"),
    ("danger", "window"),
    ("success", "window"),
    ("warning_fg", "warning_bg"),
]


def build_qpalette(p: Palette) -> QPalette:
    pal = QPalette()
    R = QPalette.ColorRole
    pal.setColor(R.Window, QColor(p.surface))
    pal.setColor(R.WindowText, QColor(p.text))
    pal.setColor(R.Base, QColor(p.window))
    pal.setColor(R.AlternateBase, QColor(p.surface_alt))
    pal.setColor(R.Text, QColor(p.text))
    pal.setColor(R.PlaceholderText, QColor(p.text_muted))
    pal.setColor(R.Button, QColor(p.surface))
    pal.setColor(R.ButtonText, QColor(p.text))
    pal.setColor(R.Highlight, QColor(p.accent))
    pal.setColor(R.HighlightedText, QColor(p.on_accent))
    pal.setColor(R.ToolTipBase, QColor(p.surface))
    pal.setColor(R.ToolTipText, QColor(p.text))
    pal.setColor(R.Link, QColor(p.link))
    pal.setColor(R.Mid, QColor(p.border))
    for role in (R.Text, R.WindowText, R.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(p.text_muted))
    return pal


def build_stylesheet(p: Palette) -> str:
    return f"""
    QSplitter::handle {{ background: {p.border}; }}
    QTreeView, QListView, QTableView {{ background: {p.surface}; border: none; }}
    QTreeView::item:selected, QListView::item:selected, QTableView::item:selected {{
        background: {p.selection}; color: {p.text};
    }}
    QPushButton[primary="true"] {{
        background: {p.accent}; color: {p.on_accent}; border: none;
        border-radius: 6px; padding: 6px 18px; font-weight: 600;
    }}
    QLabel[muted="true"] {{ color: {p.text_muted}; }}
    QLabel[error="true"] {{ color: {p.danger}; }}
    QLabel[success="true"] {{ color: {p.success}; }}
    """


class ThemeManager(QObject):
    """テーマの適用と、Windowsの設定変更への追従を行う。"""

    themeChanged = Signal(object)  # Palette

    def __init__(self, app: QApplication, mode: str = "system") -> None:
        super().__init__(app)
        self._app = app
        self._mode = mode if mode in THEME_MODES else "system"
        self._palette = LIGHT
        if sys.platform == "win32" and "windows11" in [s.lower() for s in QStyleFactory.keys()]:
            app.setStyle("windows11")
        else:
            app.setStyle("Fusion")
        app.styleHints().colorSchemeChanged.connect(self._on_system_scheme_changed)
        self.apply()

    @property
    def palette(self) -> Palette:
        return self._palette

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in THEME_MODES:
            raise ValueError(mode)
        self._mode = mode
        self.apply()

    def _system_is_dark(self) -> bool:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark

    def _on_system_scheme_changed(self, _scheme: Qt.ColorScheme) -> None:
        if self._mode == "system":
            self.apply()

    def apply(self) -> None:
        hints = self._app.styleHints()
        if self._mode == "light":
            hints.setColorScheme(Qt.ColorScheme.Light)
            dark = False
        elif self._mode == "dark":
            hints.setColorScheme(Qt.ColorScheme.Dark)
            dark = True
        else:
            hints.unsetColorScheme()
            dark = self._system_is_dark()
        new = DARK if dark else LIGHT
        self._app.setPalette(build_qpalette(new))
        self._app.setStyleSheet(build_stylesheet(new))
        changed = new is not self._palette
        self._palette = new
        if changed:
            self.themeChanged.emit(new)
