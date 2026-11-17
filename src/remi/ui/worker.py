"""UIを止めずに、重い処理を別スレッドで1回だけ実行する小さな仕組み。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class _Signals(QObject):
    finished = Signal(object)
    failed = Signal(object)


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any], signals: _Signals) -> None:
        super().__init__()
        self.fn = fn
        self.signals = signals

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as e:  # noqa: BLE001
            self.signals.failed.emit(e)
        else:
            self.signals.finished.emit(result)


def run_in_background(fn: Callable[[], Any], on_done: Callable[[Any], None],
                      on_error: Callable[[Exception], None], parent: QObject) -> _Signals:
    """fn を別スレッドで実行し、結果を UI スレッドのコールバックで受け取る。"""
    signals = _Signals(parent)
    signals.finished.connect(on_done)
    signals.failed.connect(on_error)
    QThreadPool.globalInstance().start(_Task(fn, signals))
    return signals
