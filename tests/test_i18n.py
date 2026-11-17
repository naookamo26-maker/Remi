import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from remi.i18n import SUPPORTED_LANGUAGES, install_translators, qm_path, resolve_language

ROOT = Path(__file__).resolve().parents[1]


def test_resolve_explicit_language():
    assert resolve_language("ja") == "ja"
    assert resolve_language("en") == "en"


def test_resolve_system_falls_back_to_supported():
    assert resolve_language("system") in SUPPORTED_LANGUAGES
    assert resolve_language("xx") in SUPPORTED_LANGUAGES


def test_all_strings_are_translated():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "i18n.py"), "check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_japanese_translation_loads(qapp):
    assert qm_path("ja").exists(), "run: python tools/i18n.py build"
    translators = install_translators(qapp, "ja")
    try:
        assert QCoreApplication.translate("MainWindow", "Issues") == "チケット"
    finally:
        for t in translators:
            qapp.removeTranslator(t)
    assert QCoreApplication.translate("MainWindow", "Issues") == "Issues"
