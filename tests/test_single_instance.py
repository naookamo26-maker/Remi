import subprocess
import sys
import threading
import time
import uuid

from remi.system.single_instance import SingleInstance, default_address

CLIENT = """
import sys
from remi.system.single_instance import SingleInstance
print(SingleInstance(sys.argv[1]).notify_existing())
"""


def _address(tmp_path):
    if sys.platform == "win32":
        return rf"\\.\pipe\remi-test-{uuid.uuid4().hex[:8]}"
    return str(tmp_path / "s.sock")


def test_second_instance_activates_first(tmp_path):
    addr = _address(tmp_path)
    first = SingleInstance(addr)
    assert first.notify_existing() is False
    shown = threading.Event()
    assert first.listen(lambda _msg: shown.set())
    try:
        out = subprocess.run([sys.executable, "-c", CLIENT, addr], capture_output=True, text=True, timeout=20)
        assert out.stdout.strip() == "True", out.stderr
        assert shown.wait(5)
    finally:
        first.close()


def test_default_address_is_per_user():
    assert "remi" in default_address().lower()


class _Scripted(SingleInstance):
    """notify_existing・listen の結果を順に返す(同時に起動したときの競合を再現する)。"""

    def __init__(self, notify_results, listen_result):
        super().__init__("unused")
        self.notify_results = list(notify_results)
        self.listen_result = listen_result
        self.listened = False

    def notify_existing(self, message="show"):
        return self.notify_results.pop(0)

    def listen(self, on_show):
        self.listened = True
        return self.listen_result


def test_acquire_exits_when_another_instance_is_running():
    s = _Scripted([True], listen_result=True)
    assert s.acquire(lambda: None) is False
    assert not s.listened


def test_acquire_continues_when_listening():
    assert _Scripted([False], listen_result=True).acquire(lambda: None) is True


def test_acquire_exits_when_another_instance_won_the_race():
    """同時に起動して、相手が先に待ち受けた(こちらは待ち受けられない)ときは、相手を前面に出して終了する。"""
    s = _Scripted([False, True], listen_result=False)
    assert s.acquire(lambda: None) is False


def test_acquire_continues_when_listening_fails_without_other_instance():
    assert _Scripted([False, False], listen_result=False).acquire(lambda: None) is True


OPEN_CLIENT = """
import sys
from remi.system.single_instance import SingleInstance
print(SingleInstance(sys.argv[1]).notify_existing(("open", 1, 23)))
"""


def test_second_instance_forwards_open_request(tmp_path):
    """通知を押して起動された2つ目のプロセスは、開くチケットを最初のプロセスへ渡す。"""
    addr = _address(tmp_path)
    first = SingleInstance(addr)
    got = []
    done = threading.Event()
    assert first.listen(lambda msg: (got.append(msg), done.set()))
    try:
        out = subprocess.run([sys.executable, "-c", OPEN_CLIENT, addr], capture_output=True, text=True, timeout=20)
        assert out.stdout.strip() == "True", out.stderr
        assert done.wait(5)
        assert got == [("open", 1, 23)]
    finally:
        first.close()


def test_rejects_unknown_requests():
    from remi.system.single_instance import _valid

    assert _valid("show") and _valid(("open", 1, 2))
    assert not _valid(("open", 0, 2)) and not _valid(("open", "1", 2)) and not _valid(("open", True, 2))
    assert not _valid("open") and not _valid(("run", 1, 2)) and not _valid(None)


def test_quit_request_reaches_the_running_instance(tmp_path):
    """--quit で、起動中のRemiに終了の依頼を送れる。起動していなければ、何も起きない(False)。"""
    from remi.system.single_instance import SingleInstance

    address = str(tmp_path / "remi.sock") if sys.platform != "win32" else rf"\\.\pipe\Remi-test-{uuid.uuid4().hex}"
    assert SingleInstance(address).notify_existing("quit") is False
    got = []
    first = SingleInstance(address)
    assert first.listen(lambda msg="show": got.append(msg))
    try:
        assert SingleInstance(address).notify_existing("quit") is True
        end = time.monotonic() + 5
        while time.monotonic() < end and not got:
            time.sleep(0.05)
        assert got == ["quit"]
    finally:
        first.close()


def test_quit_option_is_parsed():
    from remi.app import parse_args

    assert parse_args(["remi", "--quit"]).quit is True
    assert parse_args(["remi"]).quit is False
