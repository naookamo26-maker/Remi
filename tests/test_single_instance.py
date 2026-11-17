import uuid

from PySide6.QtCore import QEventLoop, QTimer

from remi.system.single_instance import SingleInstance


def test_second_instance_activates_first(qapp):
    name = f"remi-test-{uuid.uuid4().hex[:8]}"
    first = SingleInstance(name)
    assert first.notify_existing(100) is False
    assert first.listen()
    hits = []
    first.activateRequested.connect(lambda: hits.append(1))

    second = SingleInstance(name)
    assert second.notify_existing() is True

    loop = QEventLoop()
    first.activateRequested.connect(loop.quit)
    QTimer.singleShot(2000, loop.quit)
    loop.exec()
    first.close()
    assert hits
