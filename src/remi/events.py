"""アプリ内のイベントバス(設計書 §2.1)。

同期ワーカーなどが publish したイベントを、
- 画面(SSEの購読ごとのキュー)
- アプリ内の処理(通知、トレイなどのコールバック)
へ配る。どのスレッドから publish してもよい。
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

Event = dict[str, Any]


class Subscription:
    def __init__(self, maxsize: int = 1000) -> None:
        self.queue: queue.Queue[Event] = queue.Queue(maxsize)

    def overflow(self) -> None:
        """あふれたとき: 溜まった古いイベントをすべて捨て、全体の再読み込み(resync)だけを残す。"""
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break
        try:
            self.queue.put_nowait({"type": "resync"})
        except queue.Full:  # 空にした直後に他のスレッドが詰めた。その中身は古いので、もう一度空にする
            self.overflow()

    def get(self, timeout: float | None = None) -> Event | None:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: list[Subscription] = []
        self._callbacks: list[Callable[[Event], None]] = []
        # サーバーごとの、今動いている同期の種類(例: {1: {"versions"}})。画面の「読み込み中」の表示に使う
        self._activity: dict[int, set[str]] = {}

    def set_activity(self, server_id: int, name: str, on: bool) -> None:
        """同期の活動(name)が始まった・終わったことを記録し、画面に知らせる。"""
        with self._lock:
            current = self._activity.setdefault(server_id, set())
            if (name in current) == on:
                return
            current.add(name) if on else current.discard(name)
        self.publish("syncState", serverId=server_id)

    def activity(self, server_id: int) -> set[str]:
        with self._lock:
            return set(self._activity.get(server_id, ()))

    def subscribe(self) -> Subscription:
        sub = Subscription()
        with self._lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def on(self, callback: Callable[[Event], None]) -> None:
        with self._lock:
            self._callbacks.append(callback)

    def publish(self, type_: str, **data: Any) -> None:
        event: Event = {"type": type_, **data}
        with self._lock:
            subs = list(self._subs)
            callbacks = list(self._callbacks)
        for sub in subs:
            try:
                sub.queue.put_nowait(event)
            except queue.Full:
                # 画面が長く止まっている: 古いイベントを捨て、全体の再読み込みだけを残す
                sub.overflow()
        for cb in callbacks:
            try:
                cb(event)
            except Exception:  # noqa: BLE001
                log.exception("event callback failed for %s", type_)
