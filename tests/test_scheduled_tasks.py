"""定期的なタスク(設計書 §8.1)。"""

import json
import sys
from pathlib import Path

import pytest

from remi import __version__
from remi.store.settings import Settings
from remi.system import scheduled_tasks as st
from remi.system.scheduled_tasks import ScheduledTask, TaskScheduler, expand, parse, resolve_command

ROOT = Path(__file__).resolve().parents[1]


def task(**kw):
    base = {"name": "t", "command": ["tool.exe"], "interval_minutes": 60}
    return {**base, **kw}


# --- 設定の読み方 -----------------------------------------------------------------------
def test_sample_file_is_valid_and_is_not_read():
    sample = ROOT / "src" / "remi" / "scheduled_tasks.sample.json"
    tasks, errors = parse(json.loads(sample.read_text(encoding="utf-8")))
    assert tasks and not errors
    assert st.TASKS_FILE.name == "scheduled_tasks.json"  # 見本は読まない


def test_parse_defaults_and_minimum_interval():
    [t] = parse({"tasks": [task(interval_minutes=1)]})[0]
    assert t == ScheduledTask("t", ("tool.exe",), st.MIN_INTERVAL_MINUTES, False, st.DEFAULT_TIMEOUT)


@pytest.mark.parametrize("bad", [
    task(name=""), task(name="a/b"), task(command=[]), task(command="tool.exe"), task(command=["tool.exe", 1]),
    task(interval_minutes=0), task(interval_minutes="60"), task(interval_minutes=True), task(timeout_seconds=0),
    task(timeout_seconds=99999), task(run_at_startup="yes"), task(extra=1), task(command=["tool.exe", "{user}"]),
])
def test_parse_rejects_wrong_items(bad):
    tasks, errors = parse({"tasks": [bad, task(name="ok")]})
    assert [t.name for t in tasks] == ["ok"] and len(errors) == 1  # 誤りのある項目だけを使わない


def test_parse_rejects_duplicate_names_and_wrong_shape():
    tasks, errors = parse({"tasks": [task(), task()]})
    assert len(tasks) == 1 and "重なって" in errors[0]
    assert parse([])[0] == [] and parse({"tasks": {}})[1]


def test_placeholders_in_arguments():
    base = Path("/apps/remi")
    assert expand("{app_dir}/../logs", base) == f"{base}/../logs"
    assert expand("v{version}", base) == f"v{__version__}"
    assert expand("{{literal}}", base) == "{literal}"
    with pytest.raises(ValueError):
        expand("{other}", base)


def test_relative_program_is_resolved_from_the_app_folder(tmp_path):
    base = tmp_path / "apps" / "remi"
    t = ScheduledTask("t", ("../tools/run.exe", "a b", "{app_dir}"), 60)
    cmd = resolve_command(t, base)
    assert cmd == [str((tmp_path / "apps" / "tools" / "run.exe").resolve()), "a b", str(base)]
    absolute = ScheduledTask("t", (str(tmp_path / "x.exe"),), 60)
    assert resolve_command(absolute, base)[0] == str(tmp_path / "x.exe")


def test_no_settings_means_nothing_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "TASKS_FILE", tmp_path / "scheduled_tasks.json")
    monkeypatch.delitem(sys.modules, st.EMBEDDED_MODULE, raising=False)
    assert st.load_tasks() == []
    (tmp_path / "scheduled_tasks.json").write_text(json.dumps({"tasks": [task()]}), encoding="utf-8")
    assert [t.name for t in st.load_tasks()] == ["t"]
    (tmp_path / "scheduled_tasks.json").write_text("{broken", encoding="utf-8")
    assert st.load_tasks() == []


def test_embedded_settings_are_used_and_files_are_ignored_in_the_exe(monkeypatch, tmp_path):
    module = type(sys)(st.EMBEDDED_MODULE)
    module.DATA = json.dumps({"tasks": [task(name="embedded")]})
    monkeypatch.setitem(sys.modules, st.EMBEDDED_MODULE, module)
    assert [t.name for t in st.load_tasks()] == ["embedded"]
    # 配布物で埋め込みがなければ、ファイルがあっても読まない(利用者には開放しない)
    monkeypatch.delitem(sys.modules, st.EMBEDDED_MODULE)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(st, "TASKS_FILE", tmp_path / "scheduled_tasks.json")
    (tmp_path / "scheduled_tasks.json").write_text(json.dumps({"tasks": [task()]}), encoding="utf-8")
    assert st.load_tasks() == []


# --- 実行の間隔 ---------------------------------------------------------------------------
class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_interval_and_startup_run(db):
    settings = Settings(db)
    clock = Clock()
    runs = []
    tasks = [ScheduledTask("daily", ("x",), 1440, run_at_startup=True), ScheduledTask("hourly", ("x",), 60)]
    s = TaskScheduler(settings, tasks, base=Path("."), runner=lambda t, b: runs.append(t.name), clock=clock)
    assert s.run_due(startup=True) == ["daily"]  # 起動したら、run_at_startup のものだけすぐ実行する
    clock.now += 59 * 60
    assert s.run_due() == []
    clock.now += 60
    assert s.run_due() == ["hourly"]
    clock.now += 23 * 3600
    assert s.run_due() == ["daily", "hourly"]  # スリープ明けなどで過ぎていれば、すぐ実行する

    # 再起動しても、前回の実行時刻を引き継ぐ
    runs.clear()
    s2 = TaskScheduler(settings, [ScheduledTask("hourly", ("x",), 60)], base=Path("."),
                       runner=lambda t, b: runs.append(t.name), clock=clock)
    assert s2.run_due(startup=True) == []
    clock.now += 3600
    assert s2.run_due() == ["hourly"]


def test_failing_task_does_not_stop_others(db):
    def runner(t, b):
        if t.name == "bad":
            raise RuntimeError("boom")

    clock = Clock()
    s = TaskScheduler(Settings(db), [ScheduledTask("bad", ("x",), 60, True), ScheduledTask("good", ("x",), 60, True)],
                      base=Path("."), runner=runner, clock=clock)
    assert s.run_due(startup=True) == ["bad", "good"]


def test_scheduler_without_tasks_does_not_start_a_thread(db):
    s = TaskScheduler(Settings(db), [])
    s.start()
    assert s._thread is None


# --- コマンドの実行 -------------------------------------------------------------------------
def test_run_command_passes_arguments_as_is(tmp_path):
    out = tmp_path / "out.json"
    script = tmp_path / "write_args.py"
    script.write_text("import json, os, sys\n"
                      "json.dump({'args': sys.argv[1:], 'cwd': os.getcwd()}, open(sys.argv[1], 'w'))\n", encoding="utf-8")
    t = ScheduledTask("t", (sys.executable, str(script), str(out), "a b", "{version}", "|", "%PATH%"), 60)
    assert st.run_command(t, tmp_path) == 0
    got = json.loads(out.read_text())
    assert got["args"] == [str(out), "a b", __version__, "|", "%PATH%"]  # シェルを通さない
    assert Path(got["cwd"]).resolve() == tmp_path.resolve()


def test_run_command_missing_program_and_timeout(tmp_path):
    assert st.run_command(ScheduledTask("t", ("missing.exe",), 60), tmp_path) is None
    slow = ScheduledTask("t", (sys.executable, "-c", "import time; time.sleep(10)"), 60, timeout_seconds=1)
    assert st.run_command(slow, tmp_path) is None


# --- ビルドへの埋め込み ---------------------------------------------------------------------
def test_build_embeds_settings_only_when_present(tmp_path):
    from test_build import load_build

    b = load_build()
    out = tmp_path / "generated"
    assert b.write_embedded_tasks(out, tmp_path / "scheduled_tasks.json") is False
    assert not out.exists()
    f = tmp_path / "scheduled_tasks.json"
    f.write_text(json.dumps({"tasks": [task()]}, ensure_ascii=False), encoding="utf-8")
    assert b.write_embedded_tasks(out, f) is True
    namespace = {}
    exec((out / f"{st.EMBEDDED_MODULE}.py").read_text(encoding="utf-8"), namespace)  # noqa: S102
    assert json.loads(namespace["DATA"]) == {"tasks": [task()]}
    f.unlink()
    assert b.write_embedded_tasks(out, f) is False and not out.exists()  # 前のビルドのものを残さない
    f.write_text(json.dumps({"tasks": [task(interval_minutes=0)]}), encoding="utf-8")
    with pytest.raises(SystemExit):
        b.write_embedded_tasks(out, f)  # 誤りがあれば、ビルドを止める
