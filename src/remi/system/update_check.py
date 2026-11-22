"""更新の確認(設計書 §2.5)。Remi.exe のフォルダが SVN・Git で管理されているとき、サーバーに新しいファイルがあるかを
起動したときと6時間ごとに確かめ、あれば画面に知らせる(updateAvailable)。取り込みと再起動は Remi-update.bat が行う。

- ファイルは書き換えない。Git は `git fetch`(サーバーの情報だけを取り込む)のあと、手元より新しいコミットのうち
  このフォルダに関係するものがあるかを数える。SVN は `svn status -u` で、サーバーで新しくなったものがあるかを問い合わせる
- どちらで管理されているかは、Remi-update.bat と同じ順(Git → SVN)で、そのツールに問い合わせて決める
- どちらでもない・コマンドがない・つながらないときは、何もしない(ログに残すだけ。次の回にまた確かめる)
- 配布物(exe)のときだけ確かめる(開発中は確かめない)
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from remi.events import EventBus

log = logging.getLogger(__name__)

CHECK_INTERVAL = 6 * 3600  # 確かめる間隔(秒)
FIRST_CHECK_DELAY = 20.0  # 起動してから、最初に確かめるまで(起動の処理の邪魔をしない)
COMMAND_TIMEOUT = 120  # 1つのコマンドの時間制限(秒)
UPDATE_BATCH = "Remi-update.bat"
# svn status -u の行のうち、9文字目が * のもの(サーバーで新しくなった)
_SVN_NEWER = re.compile(r"^.{8}\*", re.M)

# コマンドを実行して (終了コード, 標準出力) を返す。実行できなければ None
Runner = Callable[[Sequence[str], Path], "tuple[int, str] | None"]


def run(cmd: Sequence[str], cwd: Path) -> tuple[int, str] | None:
    """ウィンドウを出さずに、入力なし・時間制限つきで実行する。認証の入力を求めて止まらないようにする。"""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        done = subprocess.run(list(cmd), cwd=cwd, env=env, timeout=COMMAND_TIMEOUT, creationflags=flags,  # noqa: S603
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, encoding="utf-8", errors="replace", check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.info("update check: %s failed: %s", cmd[0], e)
        return None
    return done.returncode, done.stdout


class UpdateChecker:
    """folder(Remi.exe のフォルダ)に、取り込んでいない更新があるかを確かめる。"""

    def __init__(self, folder: Path, runner: Runner = run) -> None:
        self.folder = folder
        self.runner = runner

    def detect(self) -> str | None:
        """管理しているもの: "git" / "svn" / None。Remi-update.bat と同じく Git を先に調べる。"""
        got = self.runner(["git", "rev-parse", "--is-inside-work-tree"], self.folder)
        if got is not None and got[0] == 0 and got[1].strip() == "true":
            return "git"
        got = self.runner(["svn", "info", "--non-interactive", "."], self.folder)
        if got is not None and got[0] == 0:
            return "svn"
        return None

    def check(self) -> bool | None:
        """更新があれば True、なければ False、分からなければ(管理していない・つながらない)None。"""
        vcs = self.detect()
        if vcs == "git":
            if (got := self.runner(["git", "fetch", "--quiet"], self.folder)) is None or got[0] != 0:
                return None
            # 上流のブランチにあって手元にないコミットのうち、このフォルダに関係するもの
            got = self.runner(["git", "rev-list", "--count", "HEAD..@{u}", "--", "."], self.folder)
            if got is None or got[0] != 0:
                return None
            try:
                return int(got[1].strip() or "0") > 0
            except ValueError:
                return None
        if vcs == "svn":
            got = self.runner(["svn", "status", "--show-updates", "--non-interactive", "."], self.folder)
            if got is None or got[0] != 0:
                return None
            return bool(_SVN_NEWER.search(got[1]))
        return None


class UpdateWatcher:
    """起動したときと6時間ごとに確かめ、更新が見つかったら画面に知らせる。「あとで」を押したら、次の起動までは知らせない。"""

    def __init__(self, bus: EventBus, checker: UpdateChecker | None, batch: Path | None) -> None:
        self.bus = bus
        self.checker = checker
        self.batch = batch  # Remi-update.bat(なければ、更新して再起動はできない)
        self.available = False
        self.dismissed = False
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def state(self) -> dict[str, bool]:
        with self._lock:
            return {"available": self.available and not self.dismissed,
                    "canUpdate": self.batch is not None and self.batch.is_file()}

    def dismiss(self) -> None:
        with self._lock:
            self.dismissed = True

    def check_now(self) -> None:
        if self.checker is None:
            return
        try:
            found = self.checker.check()
        except Exception:  # noqa: BLE001 - 確かめられなくても、Remi は止めない
            log.exception("update check failed")
            return
        if found is None:
            return
        with self._lock:
            changed = found != self.available
            self.available = found
        if changed:
            if found:
                log.info("an update is available in %s", self.checker.folder)
            self.bus.publish("updateAvailable", **self.state())

    def start(self) -> None:
        if self.checker is None:
            return
        threading.Thread(target=self._loop, name="update-check", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        if self._stop.wait(FIRST_CHECK_DELAY):
            return
        while True:
            self.check_now()
            if self._stop.wait(CHECK_INTERVAL):
                return

    def launch_update(self, opener: Callable[[Path], None] | None = None) -> None:
        """Remi-update.bat を起動する(バッチが Remi を終了し、取り込み、起動し直す)。"""
        if self.batch is None or not self.batch.is_file():
            raise FileNotFoundError(UPDATE_BATCH)
        (opener or open_batch)(self.batch)


def open_batch(batch: Path) -> None:
    """バッチを、Remi とは別に起動する(Remi が終了しても動き続ける)。ウィンドウは最小化して出す(失敗したときに分かるように)。"""
    if sys.platform != "win32":
        raise OSError("not supported")
    # SW_SHOWMINNOACTIVE(7): 最小化して、前面には出さない
    os.startfile(str(batch), "open", "/nopause", str(batch.parent), 7)  # noqa: S606


def create_watcher(bus: EventBus) -> UpdateWatcher:
    """配布物(exe)なら、Remi.exe のフォルダを確かめる。開発中は確かめない。"""
    if not getattr(sys, "frozen", False):
        return UpdateWatcher(bus, None, None)
    folder = Path(sys.executable).resolve().parent
    return UpdateWatcher(bus, UpdateChecker(folder), folder / UPDATE_BATCH)
