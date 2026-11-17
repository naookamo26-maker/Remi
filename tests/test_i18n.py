import json
import re
from pathlib import Path

import pytest

from remi.i18n import SUPPORTED_LANGUAGES, catalog, resolve_language, t

I18N = Path(__file__).resolve().parents[1] / "src" / "remi" / "i18n"
WEB = Path(__file__).resolve().parents[1] / "src" / "remi" / "web"


def test_catalogs_have_same_keys():
    en = catalog("en")
    for lang in SUPPORTED_LANGUAGES:
        assert set(catalog(lang)) == set(en), lang


def test_placeholders_match():
    en = catalog("en")
    for lang in SUPPORTED_LANGUAGES:
        for key, value in catalog(lang).items():
            def names(v):
                vals = v.values() if isinstance(v, dict) else [v]
                return {n for x in vals for n in re.findall(r"\{(\w+)\}", x)}
            assert names(value) == names(en[key]), (lang, key)


def test_keys_used_in_web_exist():
    used = set()
    for js in WEB.rglob("*.js"):
        used |= set(re.findall(r"""\bt\(\s*["']([\w.\-]+)["']""", js.read_text(encoding="utf-8")))
    missing = used - set(catalog("en"))
    assert not missing, missing


def test_translate_with_params_and_fallback():
    assert t("details.attachmentAdded", "ja", name="a.png") == "ファイル a.png を追加"
    assert t("no.such.key", "ja") == "no.such.key"


@pytest.mark.parametrize("setting,env,expected", [
    ("ja", "", "ja"),
    ("en", "ja_JP.UTF-8", "en"),
    ("system", "ja_JP.UTF-8", "ja"),
    ("system", "Japanese_Japan.932", "ja"),
    ("system", "fr_FR.UTF-8", "en"),
])
def test_resolve_language(monkeypatch, setting, env, expected):
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("locale.getlocale", lambda: (None, None))
    if env:
        monkeypatch.setenv("LANG", env)
    assert resolve_language(setting) == expected


def test_catalog_files_are_sorted_json():
    for f in I18N.glob("*.json"):
        data = json.loads(f.read_text(encoding="utf-8"))
        assert list(data) == sorted(data), f.name
