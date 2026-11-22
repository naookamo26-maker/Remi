"""定期的なタスク: Remi が動いている間だけ、決まった間隔でコマンドを実行する(設計書 §8.1)。

タスクは、開発者がリポジトリの src/remi/scheduled_tasks.json に書く(利用者には開放しない)。
書き方の見本は src/remi/scheduled_tasks.sample.json。このファイルは読まないので、使うときに
scheduled_tasks.json という名前で置く。scheduled_tasks.json がなければ、何も実行しない。

- 配布物(exe)では、ビルド(tools/build.py)が scheduled_tasks.json を Python のモジュール
  (EMBEDDED_MODULE)にして exe の中に埋め込む。配布フォルダに、書き換えられる設定ファイルとしては置かない
- 開発中(python -m remi)は、src/remi/scheduled_tasks.json をそのまま読む
"""

from __future__ import annotations

import json
import logging
import re
import string
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from remi import __version__
from remi.store.settings import Settings

log = logging.getLogger(__name__)

TASKS_FILE = Path(__file__).resolve().parents[1] / "scheduled_tasks.json"
# ビルドが作る、埋め込み用のモジュール(DATA に scheduled_tasks.json の中身の文字列を持つ)
EMBEDDED_MODULE = "remi_scheduled_tasks_embedded"
MIN_INTERVAL_MINUTES = 5  # これより短い間隔は、これに切り上げる
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 3600
CHECK_SECONDS = 30.0  # 実行する時刻が来たかを確かめる間隔
_NAME = re.compile(r"^[\w .-]{1,64}$")
PLACEHOLDERS = ("app_dir", "version")


@dataclass(frozen=True)
class ScheduledTask:
    name: str
    command: tuple[str, ...]  # プログラムと引数(引数の {app_dir} {version} は、実行するときに置き換える)
    interval_minutes: int
    run_at_startup: bool = False
    timeout_seconds: int = DEFAULT_TIMEOUT

    @property
    def interval_seconds(self) -> float:
        return self.interval_minutes * 60.0


def app_dir() -> Path:
    """相対パスの基準: 配布物では Remi.exe のフォルダ、開発中はリポジトリの最上位(src の1つ上)。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


def parse(data: Any) -> tuple[list[ScheduledTask], list[str]]:
    """設定(JSONを読んだもの)をタスクにする。戻り値は (タスク, 誤りの説明)。誤りのある項目は使わない。"""
    if not isinstance(data, dict) or not isinstance(data.get("tasks", []), list):
        return [], ['"tasks" にタスクのリストを書いてください']
    tasks: list[ScheduledTask] = []
    errors: list[str] = []
    names: set[str] = set()
    for i, item in enumerate(data.get("tasks", [])):
        where = f"tasks[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: オブジェクトで書いてください")
            continue
        unknown = set(item) - {"name", "command", "interval_minutes", "run_at_startup", "timeout_seconds", "説明"}
        if unknown:
            errors.append(f"{where}: 知らない項目 {sorted(unknown)}")
            continue
        name = item.get("name")
        command = item.get("command")
        interval = item.get("interval_minutes")
        timeout = item.get("timeout_seconds", DEFAULT_TIMEOUT)
        startup = item.get("run_at_startup", False)
        if not isinstance(name, str) or not _NAME.match(name):
            errors.append(f"{where}: name は64文字までの名前にしてください")
            continue
        if name in names:
            errors.append(f"{where}: name {name!r} が重なっています")
            continue
        if (not isinstance(command, list) or not command
                or not all(isinstance(x, str) and x and "\0" not in x for x in command)):
            errors.append(f"{where} ({name}): command は、プログラムと引数の文字列のリストにしてください")
            continue
        try:
            for arg in command[1:]:
                expand(arg, Path("."))
        except ValueError as e:
            errors.append(f"{where} ({name}): {e}")
            continue
        if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
            errors.append(f"{where} ({name}): interval_minutes は正の整数(分)にしてください")
            continue
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 0 < timeout <= MAX_TIMEOUT:
            errors.append(f"{where} ({name}): timeout_seconds は1〜{MAX_TIMEOUT}の整数(秒)にしてください")
            continue
        if not isinstance(startup, bool):
            errors.append(f"{where} ({name}): run_at_startup は true か false にしてください")
            continue
        if interval < MIN_INTERVAL_MINUTES:
            log.warning("scheduled task %s: interval_minutes %s is raised to %s", name, interval, MIN_INTERVAL_MINUTES)
            interval = MIN_INTERVAL_MINUTES
        names.add(name)
        tasks.append(ScheduledTask(name, tuple(command), interval, startup, timeout))
    return tasks, errors


def expand(arg: str, base: Path) -> str:
    """引数の {app_dir} {version} を置き換える。{{ }} は { } にする。ほかの {…} は誤り(ValueError)。"""
    values = {"app_dir": str(base), "version": __version__}
    out = []
    for literal, field, spec, conv in string.Formatter().parse(arg):
        out.append(literal)
        if field is None:
            continue
        if field not in values or spec or conv:
            raise ValueError(f"引数の {{{field}}} は使えません(使えるのは {', '.join('{' + p + '}' for p in PLACEHOLDERS)})")
        out.append(values[field])
    return "".join(out)


def resolve_command(task: ScheduledTask, base: Path) -> list[str]:
    """実行するコマンド。プログラムの相対パスは base(Remi.exe のフォルダ)から解決する(../ で親のフォルダ)。"""
    program = Path(task.command[0])
    if not program.is_absolute():
        program = (base / program).resolve()
    return [str(program), *(expand(a, base) for a in task.command[1:])]


def read_text() -> str | None:
    """設定の文字列。exe に埋め込んだものがあれば、それを使う。なければ(開発中は)ファイルを読む。どちらもなければ None。"""
    try:
        module = __import__(EMBEDDED_MODULE)
        return str(module.DATA)
    except ImportError:
        pass
    if getattr(sys, "frozen", False):
        return None  # 配布物では、配布フォルダのファイルは読まない(利用者には開放しない)
    try:
        return TASKS_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def load_tasks() -> list[ScheduledTask]:
    """設定を読む。なければ、何も実行しない。誤りは記録して、その項目だけを使わない。"""
    text = read_text()
    if text is None:
        return []
    try:
        tasks, errors = parse(json.loads(text))
    except ValueError:
        log.warning("cannot read the scheduled tasks", exc_info=True)
        return []
    for e in errors:
        log.warning("scheduled task ignored: %s", e)
    return tasks


def run_command(task: ScheduledTask, base: Path) -> int | None:
    """コマンドを1回実行する。ウィンドウは出さない。終了コードを返す(実行できなかった・時間切れは None)。"""
    cmd = resolve_command(task, base)
    if not Path(cmd[0]).is_file():
        log.warning("scheduled task %s: %s is not found", task.name, cmd[0])
        return None
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        done = subprocess.run(cmd, cwd=base, timeout=task.timeout_seconds, creationflags=flags,  # noqa: S603
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              check=False)
    except subprocess.TimeoutExpired:
        log.warning("scheduled task %s: did not finish in %s seconds (stopped)", task.name, task.timeout_seconds)
        return None
    except OSError as e:
        log.warning("scheduled task %s: cannot run %s: %s", task.name, cmd[0], e)
        return None
    log.info("scheduled task %s: %s exited with %s", task.name, cmd[0], done.returncode)
    return done.returncode


class TaskScheduler:
    """Remi が動いている間、タスクを間隔ごとに実行する。前回の実行時刻はタスクごとに保存し、再起動しても引き継ぐ
    (時刻は実際の時計で比べるので、スリープ明けなどで過ぎていれば、すぐ実行する)。"""

    def __init__(self, settings: Settings, tasks: list[ScheduledTask], *, base: Path | None = None,
                 runner: Callable[[ScheduledTask, Path], Any] = run_command,
                 clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.tasks = tasks
        self.base = base or app_dir()
        self.runner = runner
        self.clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def key(task: ScheduledTask) -> str:
        return f"scheduled_task_last_run:{task.name}"

    def start(self) -> None:
        """起動したときの実行(run_at_startup)を済ませてから、間隔ごとの確認を始める(別のスレッドで)。"""
        if not self.tasks:
            return
        self._thread = threading.Thread(target=self._loop, name="scheduled-tasks", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        self.run_due(startup=True)
        while not self._stop.wait(CHECK_SECONDS):
            self.run_due()

    def run_due(self, startup: bool = False) -> list[str]:
        """時刻が来たタスクを実行する。実行したタスクの名前を返す。"""
        ran = []
        for task in self.tasks:
            if self._stop.is_set():
                break
            now = self.clock()
            last = self.settings.get(self.key(task))
            if startup and task.run_at_startup:
                due = True
            elif last is None:
                # 初めて: 起動したときに実行しないタスクは、今から間隔を数え始める
                self.settings.set(self.key(task), now)
                continue
            else:
                due = now - float(last) >= task.interval_seconds or now < float(last)  # 時計が戻ったときも実行する
            if not due:
                continue
            # 先に時刻を記録する(実行中に終了しても、次の起動ですぐ繰り返さない)
            self.settings.set(self.key(task), now)
            try:
                self.runner(task, self.base)
            except Exception:  # noqa: BLE001 - タスクの失敗で、Remi を止めない
                log.exception("scheduled task %s failed", task.name)
            ran.append(task.name)
        return ran
