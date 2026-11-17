"""多言語対応(設計書 §10)。

翻訳はキー方式のJSONカタログ(en.json / ja.json)で持ち、画面とPythonで共通に使う。
"""

from __future__ import annotations

import json
import locale
import logging
import os
import sys
from functools import cache
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

I18N_DIR = Path(__file__).resolve().parent

# 言語コード -> その言語での表示名(言語選択欄では翻訳しない)
SUPPORTED_LANGUAGES: dict[str, str] = {"ja": "日本語", "en": "English"}
FALLBACK_LANGUAGE = "en"


@cache
def catalog(lang: str) -> dict[str, Any]:
    path = I18N_DIR / f"{lang}.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _system_language_codes() -> list[str]:
    codes: list[str] = []
    if sys.platform == "win32":
        try:
            import ctypes

            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            name = locale.windows_locale.get(lcid)
            if name:
                codes.append(name)
        except Exception:  # noqa: BLE001
            log.debug("GetUserDefaultUILanguage failed", exc_info=True)
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        if os.environ.get(var):
            codes.extend(os.environ[var].split(":"))
    try:
        loc = locale.getlocale()[0]
        if loc:
            codes.append(loc)
    except ValueError:
        pass
    return codes


def resolve_language(setting: str | None) -> str:
    """設定値("system" / 言語コード)から、実際に使う言語コードを決める。"""
    if setting in SUPPORTED_LANGUAGES:
        return setting  # type: ignore[return-value]
    for name in _system_language_codes():
        code = name.replace("-", "_").split("_")[0].split(".")[0].lower()
        if code in SUPPORTED_LANGUAGES:
            return code
        if code.startswith("japanese"):  # Windows の "Japanese_Japan.932" 形式
            return "ja"
    return FALLBACK_LANGUAGE


def t(key: str, lang: str, /, **params: Any) -> str:
    """翻訳した文言を返す。見つからなければ英語、それもなければキーを返す。"""
    value = catalog(lang).get(key)
    if value is None:
        value = catalog(FALLBACK_LANGUAGE).get(key, key)
    if isinstance(value, dict):  # 複数形: {"one": ..., "other": ...}
        n = params.get("count", 0)
        value = value.get("one" if (n == 1 and lang == "en") else "other", value.get("other", key))
    try:
        return str(value).format(**params)
    except (KeyError, IndexError):
        return str(value)
