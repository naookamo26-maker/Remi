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
        # 複数のスレッドが同時に publish しても、あふれたときの「空にして resync だけを残す」を、1つずつ行う
        self._put_lock = threading.Lock()

    def put(self, event: Event) -> None:
        """イベントを入れる。いっぱいなら、古いイベントをすべて捨て、全体の再読み込み(resync)だけを残す。

        入れる側(publish するスレッド)は、購読ごとの鍵で1つずつにする。取り出す側は鍵を使わない。
        鍵の中で入れるのは入れる側だけなので、空にしたあとの resync は必ず入る(通常のイベントと混ざらず、重ならない)。"""
        with self._put_lock:
            try:
                self.queue.put_nowait(event)
                return
            except queue.Full:
                pass
            while True:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    break
            self.queue.put_nowait({"type": "resync"})

    def overflow(self) -> None:
        """あふれたときの処理だけを行う(古いイベントをすべて捨て、resync だけを残す)。"""
        with self._put_lock:
            while True:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    break
            self.queue.put_nowait({"type": "resync"})

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
        # サーバーごとの、次に再試行する時刻(エポック秒)。同期に失敗して待っている間だけ持つ(画面の「約N分後に再試行」に使う)
        self._retry_at: dict[int, float] = {}

    def set_activity(self, server_id: int, name: str, on: bool) -> None:
        """同期の活動(name)が始まった・終わったことを記録し、画面に知らせる。"""
        with self._lock:
            current = self._activity.setdefault(server_id, set())
            if (name in current) == on:
                return
            current.add(name) if on else current.discard(name)
        self.publish("syncState", serverId=server_id)

    def set_retry_at(self, server_id: int, when: float | None) -> None:
        with self._lock:
            if when is None:
                self._retry_at.pop(server_id, None)
            else:
                self._retry_at[server_id] = when

    def retry_at(self, server_id: int) -> float | None:
        with self._lock:
            return self._retry_at.get(server_id)

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
            sub.put(event)  # 画面が長く止まってあふれたときは、古いイベントを捨てて、全体の再読み込みだけを残す
        for cb in callbacks:
            try:
                cb(event)
            except Exception:  # noqa: BLE001
                log.exception("event callback failed for %s", type_)
