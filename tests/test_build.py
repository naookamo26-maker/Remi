import importlib.util
from pathlib import Path

import remi

ROOT = Path(__file__).resolve().parents[1]


def load_build():
    spec = importlib.util.spec_from_file_location("build", ROOT / "tools" / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_version_file_uses_package_version(tmp_path):
    b = load_build()
    major, minor, patch = (int(x) for x in remi.__version__.split("."))
    assert b.version_tuple() == (major, minor, patch, 0)
    f = tmp_path / "v.txt"
    b.write_version_file(f)
    text = f.read_text(encoding="utf-8")
    assert f"filevers=({major}, {minor}, {patch}, 0)" in text
    assert f"StringStruct('ProductVersion', '{remi.__version__}')" in text


def test_icon(tmp_path):
    b = load_build()
    f = tmp_path / "remi.ico"
    b.write_icon(f)
    assert f.read_bytes()[:4] == b"\x00\x00\x01\x00"


def test_version_option(capsys):
    import pytest

    from remi.app import parse_args

    with pytest.raises(SystemExit) as e:
        parse_args(["remi", "--version"])
    assert e.value.code == 0
    assert remi.__version__ in capsys.readouterr().out
