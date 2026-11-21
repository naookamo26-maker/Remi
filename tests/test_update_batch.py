"""更新用のバッチ(packaging/Remi-update.bat)。exe と同じ場所に置いて、終了・更新(SVN/Git)・再起動に使う。"""

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BAT = ROOT / "packaging" / "Remi-update.bat"


def test_batch_is_plain_ascii_with_crlf():
    data = BAT.read_bytes()
    data.decode("ascii")  # 文字コードに左右されないよう、ASCIIだけにする
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")


def test_batch_stops_updates_and_restarts_in_order():
    text = BAT.read_text(encoding="ascii")
    stop = text.index('"%EXE%" --quit')
    git = text.index("git pull --ff-only")
    svn = text.index("svn update --non-interactive")
    start = text.index('start "" "%EXE%" %START_ARGS%')
    assert stop < git < start and stop < svn < start
    assert "cd /d \"%~dp0\"" in text  # exe と同じ場所で動く


def test_build_puts_the_batch_next_to_the_exe():
    spec = importlib.util.spec_from_file_location("build", ROOT / "tools" / "build.py")
    source = (ROOT / "tools" / "build.py").read_text(encoding="utf-8")
    assert spec is not None and 'packaging" / "Remi-update.bat"' in source


@pytest.mark.skipif(sys.platform != "win32", reason="Windows のバッチ")
def test_batch_updates_with_git_and_starts_again(tmp_path):
    """Git で管理した配置先で、更新(git pull)して、もう一度起動する。Remi.exe の代わりに cmd.exe を置いて確かめる。"""
    def git(*args, cwd):
        subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args], cwd=cwd, check=True,
                       capture_output=True)

    origin, work, other = tmp_path / "origin.git", tmp_path / "work", tmp_path / "other"
    git("init", "--bare", str(origin), cwd=tmp_path)
    git("clone", str(origin), str(work), cwd=tmp_path)
    (work / "version.txt").write_text("1", encoding="ascii")
    git("add", ".", cwd=work)
    git("commit", "-m", "v1", cwd=work)
    git("push", "origin", "HEAD", cwd=work)
    git("clone", str(origin), str(other), cwd=tmp_path)
    (other / "version.txt").write_text("2", encoding="ascii")
    git("commit", "-am", "v2", cwd=other)
    git("push", "origin", "HEAD", cwd=other)

    shutil.copy(BAT, work / "Remi-update.bat")
    shutil.copy(Path(os.environ["COMSPEC"]), work / "Remi.exe")  # 代わりの実行ファイル(--quit は何もせず終わる)
    env = {**os.environ, "START_ARGS": '/c "echo started> started.txt"', "WAIT_SECONDS": "10"}
    r = subprocess.run(["cmd", "/c", "Remi-update.bat", "/nopause"], cwd=work, env=env, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (work / "version.txt").read_text(encoding="ascii") == "2"  # 更新された
    end = time.monotonic() + 15
    while time.monotonic() < end and not (work / "started.txt").exists():
        time.sleep(0.2)
    assert (work / "started.txt").exists()  # もう一度起動した
    assert "[3/3]" in r.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Windows のバッチ")
def test_batch_reports_missing_exe(tmp_path):
    shutil.copy(BAT, tmp_path / "Remi-update.bat")
    r = subprocess.run(["cmd", "/c", "Remi-update.bat", "/nopause"], cwd=tmp_path, capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 1
    assert "was not found" in r.stdout
