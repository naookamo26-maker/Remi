"""サーバーごとの同期ワーカー(QThread)と、それらを束ねる SyncManager(設計書 §2.1, §4.6)。

- サーバーごとに独立したスレッドで動き、1台が落ちても他に影響しない
- 1秒ごとの tick で、期限が来た処理だけを実行する
- エラーが起きたら指数バックオフする(サーバー別)
"""

from __future__ import annotations

import logging
import mimetypes
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from remi.api.client import RedmineClient
from remi.api.errors import ErrorCode, RedmineError
from remi.paths import attachment_cache_dir
from remi.store.db import Database
from remi.store.issues import IssueStore, utcnow
from remi.store.secrets import get_api_key
from remi.store.servers import ServerStore
from remi.store.settings import Settings
from remi.sync.engine import ServerSync, SyncResult

log = logging.getLogger(__name__)

TICK_MS = 1000
OPEN_ISSUE_INTERVAL = 20  # 秒
METADATA_INTERVAL = 3600
RECONCILE_INTERVAL = 3600
DELETE_CHECK_INTERVAL = 24 * 3600
INACTIVE_FACTOR = 2
BACKOFF_BASE = 30
BACKOFF_MAX = 1800
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/bmp", "image/webp"}


@dataclass
class Schedule:
    """各処理の次回実行時刻(time.monotonic 基準)。"""

    identity: float = 0.0
    metadata: float = 0.0
    changes: float = 0.0
    open_issue: float = 0.0
    reconcile: float = 0.0
    delete_check: float = 0.0
    backoff_until: float = 0.0
    failures: int = 0
    queue: list[int] = field(default_factory=list)  # journal を取得するチケット
    attachments: list[int] = field(default_factory=list)


class ServerWorker(QObject):
    dataChanged = Signal(int, object)  # server_id, set[issue_id]
    newComments = Signal(int, object)  # server_id, list[NewComment]
    stateChanged = Signal(int)  # server_id
    attachmentReady = Signal(int, int, str)  # server_id, attachment_id, local_path("" = 失敗)
    noteFinished = Signal(int, int, object)  # server_id, issue_id, RedmineError | None

    def __init__(self, db: Database, server_id: int) -> None:
        super().__init__()
        self.db = db
        self.server_id = server_id
        self.sched = Schedule()
        self.open_issue: int | None = None
        self.active = True
        self.sync: ServerSync | None = None
        self.timer: QTimer | None = None
        self._busy = False

    # --- 開始・停止(ワーカースレッドで呼ばれる) -------------------------------
    @Slot()
    def start(self) -> None:
        servers = ServerStore(self.db)
        server = servers.get(self.server_id)
        key = get_api_key(self.server_id)
        if server is None or not key:
            log.warning("server %s has no API key; worker not started", self.server_id)
            return
        client = RedmineClient(server.url, key, proxy_mode=server.proxy_mode, proxy_url=server.proxy_url)
        self.sync = ServerSync(self.db, self.server_id, client, self._retention())
        settings = Settings(self.db)
        last_delete = settings.get(f"last_delete_check:{self.server_id}") or 0
        now = time.monotonic()
        wall_elapsed = time.time() - float(last_delete)
        self.sched.delete_check = now + max(600.0, DELETE_CHECK_INTERVAL - wall_elapsed)
        self.sched.reconcile = now + 300
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(TICK_MS)
        QTimer.singleShot(0, self.tick)

    @Slot()
    def stop(self) -> None:
        if self.timer is not None:
            self.timer.stop()
        if self.sync is not None:
            self.sync.client.close()

    # --- 外部からの依頼 -------------------------------------------------------
    @Slot()
    def sync_now(self) -> None:
        self.sched.changes = 0
        self.sched.backoff_until = 0
        self.sched.failures = 0

    @Slot(object)
    def set_open_issue(self, issue_id: int | None) -> None:
        self.open_issue = issue_id
        self.sched.open_issue = 0
        if issue_id is not None and self.sync is not None \
                and not self.sync.issues.journals_synced(self.server_id, issue_id):
            self._enqueue(issue_id, front=True)

    @Slot(bool)
    def set_active(self, active: bool) -> None:
        self.active = active

    @Slot(int)
    def request_attachment(self, attachment_id: int) -> None:
        if attachment_id not in self.sched.attachments:
            self.sched.attachments.append(attachment_id)

    @Slot(int, str, bool)
    def post_note(self, issue_id: int, notes: str, private: bool) -> None:
        if self.sync is None:
            self.noteFinished.emit(self.server_id, issue_id, RedmineError(ErrorCode.CONNECTION_FAILED))
            return
        try:
            self.sync.client.add_note(issue_id, notes, private)
        except RedmineError as e:
            log.warning("posting a note to #%s failed: %s", issue_id, e)
            self.noteFinished.emit(self.server_id, issue_id, e)
            return
        self.noteFinished.emit(self.server_id, issue_id, None)
        try:
            r = self.sync.fetch_journals(issue_id, notify=False)
            self.sync.issues.mark_read(self.server_id, issue_id)
            self.dataChanged.emit(self.server_id, r.changed_issue_ids | {issue_id})
        except RedmineError as e:
            log.info("refetch after posting failed: %s", e)

    # --- tick ----------------------------------------------------------------
    def _retention(self) -> int:
        return int(Settings(self.db).get("closed_retention_days"))

    def _interval(self, base: float) -> float:
        return base * (1 if self.active else INACTIVE_FACTOR)

    def _enqueue(self, issue_id: int, front: bool = False) -> None:
        q = self.sched.queue
        if issue_id in q:
            q.remove(issue_id)
        if front:
            q.insert(0, issue_id)
        else:
            q.append(issue_id)

    @Slot()
    def tick(self) -> None:
        if self.sync is None or self._busy:
            return
        now = time.monotonic()
        if now < self.sched.backoff_until:
            return
        self._busy = True
        try:
            self._run_due(now)
        except RedmineError as e:
            self._on_error(e)
        except LookupError:
            # サーバーが削除された
            if self.timer is not None:
                self.timer.stop()
        except Exception:  # noqa: BLE001 - 予期しないエラーでもワーカーは止めない
            log.exception("unexpected error in sync worker %s", self.server_id)
            self._on_error(RedmineError(ErrorCode.INVALID_RESPONSE))
        finally:
            self._busy = False

    def _emit(self, result: SyncResult) -> None:
        ids = result.changed_issue_ids | result.unmonitored_issue_ids | result.deleted_issue_ids
        if ids:
            self.dataChanged.emit(self.server_id, ids)
        if result.new_comments:
            self.newComments.emit(self.server_id, result.new_comments)

    def _run_due(self, now: float) -> None:
        sync = self.sync
        assert sync is not None
        sync.closed_retention_days = self._retention()
        server = sync.server
        did_network = False

        if now >= self.sched.identity:
            sync.refresh_identity()
            self.sched.identity = now + METADATA_INTERVAL
            did_network = True
        if now >= self.sched.metadata:
            sync.refresh_metadata()
            self.sched.metadata = now + METADATA_INTERVAL
            self.dataChanged.emit(self.server_id, set())
            did_network = True
        if now >= self.sched.changes:
            result = sync.sync_changes()
            for iid in sorted(result.changed_issue_ids):
                if iid != self.open_issue:
                    self._enqueue(iid)
            for iid in sync.pending_journal_issue_ids(limit=200):
                if iid not in self.sched.queue:
                    self._enqueue(iid)
            self._emit(result)
            self.sched.changes = now + self._interval(server.poll_interval)
            did_network = True
        if self.open_issue is not None and now >= self.sched.open_issue:
            self._emit(sync.check_issue(self.open_issue))
            self.sched.open_issue = now + self._interval(OPEN_ISSUE_INTERVAL)
            did_network = True
        if now >= self.sched.reconcile:
            self._emit(sync.reconcile())
            self.sched.reconcile = now + RECONCILE_INTERVAL
            did_network = True
        if now >= self.sched.delete_check:
            self._emit(sync.detect_deleted())
            Settings(self.db).set(f"last_delete_check:{self.server_id}", time.time())
            self.sched.delete_check = now + DELETE_CHECK_INTERVAL
            did_network = True

        # 1 tick につき journal 1件・添付1件だけ処理する(サーバーを叩きすぎない)
        if self.sched.queue:
            iid = self.sched.queue.pop(0)
            self._emit(sync.fetch_journals(iid))
            did_network = True
        if self.sched.attachments:
            self._download_attachment(self.sched.attachments.pop(0))
            did_network = True

        if did_network:
            self._on_success()

    # --- 添付 -----------------------------------------------------------------
    def _download_attachment(self, attachment_id: int) -> None:
        sync = self.sync
        assert sync is not None
        store = IssueStore(self.db)
        a = store.get_attachment(self.server_id, attachment_id)
        if a is None or not a.content_url:
            self.attachmentReady.emit(self.server_id, attachment_id, "")
            return
        if a.local_path:
            if Path(a.local_path).exists():
                self.attachmentReady.emit(self.server_id, attachment_id, a.local_path)
                return
        ext = mimetypes.guess_extension(a.content_type or "") or ""
        dest = attachment_cache_dir(self.server_id) / f"{attachment_id}{ext}"
        try:
            sync.client.download(a.content_url, dest, MAX_ATTACHMENT_BYTES)
        except RedmineError as e:
            if e.code == ErrorCode.TOO_LARGE or not e.retryable:
                self.attachmentReady.emit(self.server_id, attachment_id, "")
                return
            raise
        store.set_attachment_cached(self.server_id, attachment_id, str(dest))
        prune_attachment_cache(self.db)
        self.attachmentReady.emit(self.server_id, attachment_id, str(dest))

    # --- 成功・失敗 -------------------------------------------------------------
    def _on_success(self) -> None:
        servers = ServerStore(self.db)
        server = servers.get(self.server_id)
        if server is None:
            return
        had_error = server.last_error is not None
        servers.update_fields(self.server_id, last_error=None, last_synced_at=utcnow())
        self.sched.failures = 0
        self.stateChanged.emit(self.server_id)
        if had_error:
            log.info("server %s recovered", self.server_id)

    def _on_error(self, err: RedmineError) -> None:
        self.sched.failures += 1
        if err.retryable:
            delay = min(BACKOFF_MAX, BACKOFF_BASE * 2 ** (self.sched.failures - 1))
        else:
            delay = BACKOFF_MAX  # キーの誤りなどはすぐには直らないので、長めに待つ
        self.sched.backoff_until = time.monotonic() + delay
        log.warning("sync error on server %s: %s; retrying in %ss", self.server_id, err, delay)
        ServerStore(self.db).update_fields(self.server_id, last_error=err.code.value)
        self.stateChanged.emit(self.server_id)


def prune_attachment_cache(db: Database, limit_mb: int | None = None) -> None:
    """画像キャッシュの合計が上限を超えたら、古いものから削除する(設計書 §5.3)。"""
    limit = (limit_mb if limit_mb is not None else int(Settings(db).get("image_cache_limit_mb"))) * 1024 * 1024
    store = IssueStore(db)
    entries = store.cached_attachments_oldest_first()
    sizes = []
    for sid, aid, path in entries:
        p = Path(path)
        sizes.append((sid, aid, p, p.stat().st_size if p.exists() else 0))
    total = sum(s[3] for s in sizes)
    for sid, aid, p, size in sizes:
        if total <= limit:
            break
        p.unlink(missing_ok=True)
        store.set_attachment_cached(sid, aid, None)
        total -= size


class SyncManager(QObject):
    """有効なサーバーごとに ServerWorker を動かす。UIスレッドで使う。"""

    dataChanged = Signal(int, object)
    newComments = Signal(int, object)
    stateChanged = Signal(int)
    attachmentReady = Signal(int, int, str)
    noteFinished = Signal(int, int, object)

    # ワーカーへの依頼(キュー経由でワーカースレッドに届く)
    _syncNow = Signal()
    _setActive = Signal(bool)

    def __init__(self, db: Database, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self._workers: dict[int, tuple[QThread, ServerWorker, _Bridge]] = {}

    def restart(self) -> None:
        self.stop_all()
        for server in ServerStore(self.db).list(enabled_only=True):
            self._start(server.server_id)

    def _start(self, server_id: int) -> None:
        thread = QThread()
        thread.setObjectName(f"sync-{server_id}")
        worker = ServerWorker(self.db, server_id)
        worker.moveToThread(thread)
        bridge = _Bridge()
        bridge.syncNow.connect(worker.sync_now)
        bridge.setOpen.connect(worker.set_open_issue)
        bridge.setActive.connect(worker.set_active)
        bridge.attachment.connect(worker.request_attachment)
        bridge.postNote.connect(worker.post_note)
        bridge.stop.connect(worker.stop)
        self._syncNow.connect(worker.sync_now)
        self._setActive.connect(worker.set_active)
        worker.dataChanged.connect(self.dataChanged)
        worker.newComments.connect(self.newComments)
        worker.stateChanged.connect(self.stateChanged)
        worker.attachmentReady.connect(self.attachmentReady)
        worker.noteFinished.connect(self.noteFinished)
        thread.started.connect(worker.start)
        thread.start()
        self._workers[server_id] = (thread, worker, bridge)

    def stop_all(self) -> None:
        for thread, worker, bridge in self._workers.values():
            bridge.stop.emit()  # タイマーを止める(ワーカースレッドで実行される)
            thread.quit()
            if not thread.wait(5000):
                # 通信中で止まらない場合は、終了を待たずに進める(タイムアウトで必ず終わる)
                log.warning("sync thread %s did not stop in time", thread.objectName())
            worker.deleteLater()
        self._workers.clear()

    # --- 依頼 -----------------------------------------------------------------
    def sync_now(self) -> None:
        self._syncNow.emit()

    def set_active(self, active: bool) -> None:
        self._setActive.emit(active)

    def set_open_issue(self, server_id: int | None, issue_id: int | None) -> None:
        for sid, (_t, _w, bridge) in self._workers.items():
            bridge.setOpen.emit(issue_id if sid == server_id else None)

    def request_attachment(self, server_id: int, attachment_id: int) -> None:
        if server_id in self._workers:
            self._workers[server_id][2].attachment.emit(attachment_id)

    def post_note(self, server_id: int, issue_id: int, notes: str, private: bool) -> bool:
        if server_id not in self._workers:
            return False
        self._workers[server_id][2].postNote.emit(issue_id, notes, private)
        return True


class _Bridge(QObject):
    syncNow = Signal()
    setOpen = Signal(object)
    setActive = Signal(bool)
    attachment = Signal(int)
    postNote = Signal(int, str, bool)
    stop = Signal()
