"""更新の確認(設計書 §2.5)。"""

from pathlib import Path

import pytest

from remi.events import EventBus
from remi.system import update_check as uc
from remi.system.update_check import UpdateChecker, UpdateWatcher


def fake(responses):
    """コマンドの先頭2語 → (終了コード, 出力)。ないものは実行できなかった(None)。呼ばれたコマンドを記録する。"""
    calls = []

    def runner(cmd, cwd):
        calls.append(list(cmd))
        return responses.get(tuple(cmd[:2]))

    runner.calls = calls
    return runner


GIT = {("git", "rev-parse"): (0, "true\n"), ("git", "fetch"): (0, "")}
SVN = {("git", "rev-parse"): (128, ""), ("svn", "info"): (0, "Path: .\n")}


def test_detects_git_first_like_the_update_batch(tmp_path):
    both = {**GIT, ("svn", "info"): (0, "")}
    assert UpdateChecker(tmp_path, fake(both)).detect() == "git"
    assert UpdateChecker(tmp_path, fake(SVN)).detect() == "svn"
    assert UpdateChecker(tmp_path, fake({})).detect() is None  # コマンドがない・管理していない


def test_git_counts_new_commits_for_this_folder(tmp_path):
    r = fake({**GIT, ("git", "rev-list"): (0, "2\n")})
    assert UpdateChecker(tmp_path, r).check() is True
    assert ["git", "rev-list", "--count", "HEAD..@{u}", "--", "."] in r.calls  # このフォルダの変更だけを数える
    assert UpdateChecker(tmp_path, fake({**GIT, ("git", "rev-list"): (0, "0\n")})).check() is False
    assert UpdateChecker(tmp_path, fake({**GIT, ("git", "fetch"): (128, "")})).check() is None  # つながらない
    assert UpdateChecker(tmp_path, fake({**GIT, ("git", "rev-list"): (128, "")})).check() is None  # 上流がない


def test_svn_looks_for_items_newer_on_the_server(tmp_path):
    newer = "        *       41   Remi.exe\nStatus against revision:     42\n"
    assert UpdateChecker(tmp_path, fake({**SVN, ("svn", "status"): (0, newer)})).check() is True
    latest = "Status against revision:     42\n"
    assert UpdateChecker(tmp_path, fake({**SVN, ("svn", "status"): (0, latest)})).check() is False
    modified = "M               41   settings.ini\nStatus against revision:     42\n"  # 手元で変えただけ
    assert UpdateChecker(tmp_path, fake({**SVN, ("svn", "status"): (0, modified)})).check() is False
    assert UpdateChecker(tmp_path, fake({**SVN, ("svn", "status"): (1, "")})).check() is None


class FixedChecker:
    def __init__(self, result):
        self.result = result
        self.folder = Path(".")

    def check(self):
        return self.result


def test_watcher_announces_once_and_later_hides_until_restart(tmp_path):
    batch = tmp_path / uc.UPDATE_BATCH
    batch.write_text("@echo off\n")
    bus = EventBus()
    events = []
    bus.on(events.append)
    checker = FixedChecker(False)
    w = UpdateWatcher(bus, checker, batch)
    w.check_now()
    assert w.state() == {"available": False, "canUpdate": True} and not events
    checker.result = True
    w.check_now()
    w.check_now()  # 2回目は知らせない
    assert [e["type"] for e in events] == ["updateAvailable"]
    assert w.state()["available"]
    w.dismiss()
    assert not w.state()["available"]  # 「あとで」: 次の起動までは出さない
    checker.result = None  # 分からなかったときは、前の状態のまま
    w.check_now()
    assert w.available


def test_watcher_announces_when_the_update_is_gone(tmp_path):
    """更新がなくなったときも知らせる(画面に出ている帯を消すため)。"""
    bus = EventBus()
    events = []
    bus.on(events.append)
    checker = FixedChecker(True)
    w = UpdateWatcher(bus, checker, tmp_path / uc.UPDATE_BATCH)
    w.check_now()
    checker.result = False
    w.check_now()
    w.check_now()  # 変わらないときは知らせない
    assert [(e["type"], e["available"]) for e in events] == [("updateAvailable", True), ("updateAvailable", False)]


def test_launch_update_needs_the_batch(tmp_path):
    w = UpdateWatcher(EventBus(), FixedChecker(True), tmp_path / uc.UPDATE_BATCH)
    assert not w.state()["canUpdate"]
    with pytest.raises(FileNotFoundError):
        w.launch_update(opener=lambda b: None)
    (tmp_path / uc.UPDATE_BATCH).write_text("@echo off\n")
    opened = []
    w.launch_update(opener=opened.append)
    assert opened == [tmp_path / uc.UPDATE_BATCH]


def test_not_checked_while_developing():
    w = uc.create_watcher(EventBus())
    assert w.checker is None and w.state() == {"available": False, "canUpdate": False}
    w.start()  # 何もしない


def test_update_rpcs(db, tmp_path):
    from remi.context import AppContext
    from remi.server.api import RemiApi, RpcError

    api = RemiApi(AppContext(db), None)
    assert api.getUpdateState() == {"available": False, "canUpdate": False}
    assert api.bootstrap()["update"] == {"available": False, "canUpdate": False}
    with pytest.raises(RpcError) as e:
        api.startUpdate()
    assert e.value.code == "update_unavailable"
    api.updates = UpdateWatcher(api.ctx.bus, FixedChecker(True), tmp_path / uc.UPDATE_BATCH)
    api.updates.check_now()
    assert api.getUpdateState() == {"available": True, "canUpdate": False}
    with pytest.raises(RpcError) as e:
        api.startUpdate()
    assert e.value.code == "update_unavailable"
    api.dismissUpdate()
    assert api.getUpdateState()["available"] is False


def test_with_real_git(tmp_path):
    """実際の Git で: 配布先(clone)の上流に、配布先のフォルダの変更が増えたら、更新ありになる。ほかのフォルダの変更は数えない。"""
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git is not installed")

    def git(*args, cwd):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True,
                       capture_output=True)

    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "main", cwd=origin)
    (origin / "remi").mkdir()
    (origin / "remi" / "Remi.exe").write_text("v1")
    (origin / "other").mkdir()
    (origin / "other" / "tool.txt").write_text("v1")
    git("add", ".", cwd=origin)
    git("commit", "-q", "-m", "v1", cwd=origin)
    git("clone", "-q", str(origin), "deploy", cwd=tmp_path)
    folder = tmp_path / "deploy" / "remi"
    checker = UpdateChecker(folder)
    assert checker.detect() == "git"
    assert checker.check() is False
    (origin / "other" / "tool.txt").write_text("v2")  # ほかのフォルダだけの変更
    git("commit", "-q", "-am", "other", cwd=origin)
    assert checker.check() is False
    (origin / "remi" / "Remi.exe").write_text("v2")
    git("commit", "-q", "-am", "remi", cwd=origin)
    assert checker.check() is True
    assert (folder / "Remi.exe").read_text() == "v1"  # ファイルは書き換えない
