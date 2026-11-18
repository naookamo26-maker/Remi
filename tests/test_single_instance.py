import subprocess
import sys
import threading
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
    assert first.listen(shown.set)
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

    def notify_existing(self):
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
