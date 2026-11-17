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
