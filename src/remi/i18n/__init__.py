"""多言語対応(設計書 §10)。

ソースコード内の文字列は英語で書き、翻訳は remi_<lang>.qm から読み込む。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
from PySide6.QtWidgets import QApplication

log = logging.getLogger(__name__)

I18N_DIR = Path(__file__).resolve().parent

# 言語コード -> その言語での表示名(言語選択欄では翻訳しない)
SUPPORTED_LANGUAGES: dict[str, str] = {
    "ja": "日本語",
    "en": "English",
}
SOURCE_LANGUAGE = "en"


def resolve_language(setting: str | None) -> str:
    """設定値("system" / 言語コード)から、実際に使う言語コードを決める。"""
    if setting and setting in SUPPORTED_LANGUAGES:
        return setting
    for name in QLocale.system().uiLanguages():
        code = name.replace("_", "-").split("-")[0].lower()
        if code in SUPPORTED_LANGUAGES:
            return code
    return SOURCE_LANGUAGE


def qm_path(lang: str) -> Path:
    return I18N_DIR / f"remi_{lang}.qm"


def install_translators(app: QApplication, lang: str) -> list[QTranslator]:
    """アプリとQt標準ダイアログの翻訳を読み込む。戻り値は参照を保持しておくこと。"""
    QLocale.setDefault(QLocale(lang))
    installed: list[QTranslator] = []
    if lang == SOURCE_LANGUAGE:
        return installed

    qt = QTranslator(app)
    if qt.load(QLocale(lang), "qtbase", "_", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(qt)
        installed.append(qt)

    own = QTranslator(app)
    path = qm_path(lang)
    if own.load(str(path)):
        app.installTranslator(own)
        installed.append(own)
    else:
        log.warning("translation file not found: %s (run tools/i18n.py build)", path)
    return installed
