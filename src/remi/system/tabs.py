"""Redmineのタブ(設計書 §5.4)。

本体のウィンドウの中に、Redmineのページをタブで表示する。この中身は、Windowsに依存しない部分:
- TabModel: タブの一覧・選択中のタブ・各タブの状態(題名・URL・戻る/進む・読み込み中)
- TabController: モデルと、実際にページを表示する部品(TabHost)をつなぐ。画面へは、変わるたびに通知する

ページを表示する部品(WebView2)は system/tabs_win.py にあり、Windows でだけ使う。部品がない環境(開発モード・テスト)では、
モデルだけで動く(画面の操作の確認用)。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)

ACTIONS = ("back", "forward", "reload")


@dataclass
class Tab:
    id: int
    url: str
    title: str = ""
    can_back: bool = False
    can_forward: bool = False
    loading: bool = True

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "url": self.url, "title": self.title, "canBack": self.can_back,
                "canForward": self.can_forward, "loading": self.loading}


class TabModel:
    """タブの一覧と選択中のタブ。active が None のときは、固定の「Remi」のタブ(本体の画面)を選んでいる。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tabs: list[Tab] = []
        self._next = 1
        self.active: int | None = None

    def open(self, url: str) -> Tab:
        """新しいタブを、いちばん右に作って選ぶ。"""
        with self._lock:
            tab = Tab(id=self._next, url=url)
            self._next += 1
            self._tabs.append(tab)
            self.active = tab.id
            return tab

    def get(self, tab_id: int) -> Tab | None:
        with self._lock:
            return next((t for t in self._tabs if t.id == tab_id), None)

    def close(self, tab_id: int) -> Tab | None:
        """タブを閉じる。選んでいたタブなら、右隣(なければ左隣、なければ「Remi」)を選ぶ。"""
        with self._lock:
            index = next((i for i, t in enumerate(self._tabs) if t.id == tab_id), None)
            if index is None:
                return None
            tab = self._tabs.pop(index)
            if self.active == tab_id:
                self.active = self._tabs[min(index, len(self._tabs) - 1)].id if self._tabs else None
            return tab

    def activate(self, tab_id: int | None) -> bool:
        """タブを選ぶ(None は「Remi」)。変わったら True。"""
        with self._lock:
            if tab_id is not None and self.get(tab_id) is None:
                return False
            changed = self.active != tab_id
            self.active = tab_id
            return changed

    def update(self, tab_id: int, **fields: Any) -> bool:
        """タブの状態を更新する。変わったら True。"""
        with self._lock:
            tab = self.get(tab_id)
            if tab is None:
                return False
            changed = False
            for key, value in fields.items():
                if getattr(tab, key) != value:
                    setattr(tab, key, value)
                    changed = True
            return changed

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"tabs": [t.to_json() for t in self._tabs], "active": self.active}


class TabHost(Protocol):
    """ページを実際に表示する部品(Windows の WebView2)。どのスレッドから呼んでもよい。"""

    def open(self, tab: Tab) -> bool: ...

    def activate(self, tab_id: int | None) -> None: ...

    def close(self, tab_id: int) -> None: ...

    def action(self, tab_id: int, action: str) -> None: ...

    def set_chrome_height(self, height: float) -> None: ...

    def dispose(self) -> None: ...


class TabController:
    """タブの操作の窓口。画面(API)・表示する部品(host)・通知(on_changed)をつなぐ。"""

    def __init__(self, on_changed: Callable[[], None] | None = None, host: TabHost | None = None,
                 on_failed: Callable[[str], None] | None = None) -> None:
        self.model = TabModel()
        self.host = host
        self.on_changed = on_changed
        self.on_failed = on_failed  # ページを開けなかったタブのURL(既定のブラウザで開くために使う)
        self.last_error: str | None = None  # タブにできなかった理由(画面で知らせ、原因を調べるために使う)

    def _changed(self) -> None:
        if self.on_changed is not None:
            try:
                self.on_changed()
            except Exception:  # noqa: BLE001 - 通知に失敗しても、タブの操作は続ける
                log.debug("tabs changed callback failed", exc_info=True)

    # --- 画面からの操作 ---------------------------------------------------------------
    def open(self, url: str) -> bool:
        """URL をタブで開く。開けなければ False(呼んだ側が、別ウィンドウなどで開く)。"""
        tab = self.model.open(url)
        if self.host is not None:
            try:
                ok = self.host.open(tab)
                if not ok:
                    self.last_error = "the tab host could not open the page"
            except Exception as e:  # noqa: BLE001
                log.exception("could not open a Redmine tab")
                self.last_error = f"{type(e).__name__}: {e}"
                ok = False
            if not ok:
                self.model.close(tab.id)
                self._sync_host_active()
                return False
        self._changed()
        return True

    def activate(self, tab_id: int | None) -> None:
        if self.model.activate(tab_id):
            self._sync_host_active()
            self._changed()

    def close(self, tab_id: int) -> None:
        if self.model.close(tab_id) is None:
            return
        if self.host is not None:
            try:
                self.host.close(tab_id)
            except Exception:  # noqa: BLE001
                log.exception("could not close a Redmine tab")
        self._sync_host_active()
        self._changed()

    def action(self, tab_id: int, action: str) -> None:
        if action not in ACTIONS or self.model.get(tab_id) is None:
            return
        if self.host is not None:
            self.host.action(tab_id, action)

    def detach(self, tab_id: int) -> str | None:
        """タブを閉じて、その URL を返す(呼んだ側が、別ウィンドウで開く)。"""
        tab = self.model.get(tab_id)
        if tab is None:
            return None
        url = tab.url
        self.close(tab_id)
        return url

    def set_chrome_height(self, height: float) -> None:
        if self.host is not None:
            self.host.set_chrome_height(height)

    # --- 表示する部品からの通知 ---------------------------------------------------------
    def update(self, tab_id: int, **fields: Any) -> None:
        if self.model.update(tab_id, **fields):
            self._changed()

    def failed(self, tab_id: int) -> None:
        """ページを表示する部品を作れなかった: タブを閉じ、既定のブラウザなどで開き直してもらう。"""
        tab = self.model.get(tab_id)
        if tab is None:
            return
        self.last_error = "WebView2 for the tab could not start"
        self.close(tab_id)
        if self.on_failed is not None:
            self.on_failed(tab.url)

    def snapshot(self) -> dict[str, Any]:
        return self.model.snapshot()

    def dispose(self) -> None:
        if self.host is not None:
            try:
                self.host.dispose()
            except Exception:  # noqa: BLE001
                log.debug("could not dispose the tab host", exc_info=True)

    def _sync_host_active(self) -> None:
        if self.host is not None:
            try:
                self.host.activate(self.model.active)
            except Exception:  # noqa: BLE001
                log.exception("could not switch the Redmine tab")
