import subprocess
import sys
import uuid

from PySide6.QtCore import QEventLoop, QTimer

from remi.system.single_instance import SingleInstance

CLIENT = """
import sys
from PySide6.QtCore import QCoreApplication
from remi.system.single_instance import SingleInstance
app = QCoreApplication([])
print(SingleInstance(sys.argv[1]).notify_existing(2000))
"""


def test_second_instance_activates_first(qapp):
    name = f"remi-test-{uuid.uuid4().hex[:8]}"
    first = SingleInstance(name)
    assert first.notify_existing(100) is False
    assert first.listen()
    hits = []
    first.activateRequested.connect(lambda: hits.append(1))

    # 2つ目のRemiを別プロセスとして起動する(実際の使われ方と同じ)
    proc = subprocess.Popen([sys.executable, "-c", CLIENT, name], stdout=subprocess.PIPE, text=True)
    loop = QEventLoop()
    first.activateRequested.connect(loop.quit)
    QTimer.singleShot(10000, loop.quit)
    loop.exec()
    out, _ = proc.communicate(timeout=10)
    first.close()
    assert hits
    assert out.strip() == "True"


def test_no_existing_instance(qapp):
    assert SingleInstance(f"remi-none-{uuid.uuid4().hex[:8]}").notify_existing(100) is False
