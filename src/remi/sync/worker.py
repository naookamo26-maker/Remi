"""サーバーごとの同期ワーカー(スレッド)と、それらを束ねる SyncManager(設計書 §2.1, §4.6)。

- サーバーごとに独立したスレッドで動き、1台が落ちても他に影響しない
- 約1秒ごとに、期限が来た処理だけを実行する
- 画面などからの依頼はコマンドキューで受け取り、ワーカースレッドで実行する
- エラーが起きたら指数バックオフする(サーバー別)
- 結果はイベントバスへ流す
"""

from __future__ import annotations

import logging
import mimetypes
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from remi.api.client import RedmineClient
from remi.api.errors import ErrorCode, RedmineError
from remi.events import EventBus
from remi.paths import attachment_cache_dir
from remi.store.db import Database
from remi.store.issues import IssueStore, utcnow
from remi.store.secrets import get_api_key
from remi.store.servers import ServerStore
from remi.store.settings import Settings
from remi.sync.engine import ServerSync, SyncResult

log = logging.getLogger(__name__)

TICK_SECONDS = 1.0
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


class ServerWorker(threading.Thread):
    def __init__(self, db: Database, server_id: int, bus: EventBus) -> None:
        super().__init__(name=f"sync-{server_id}", daemon=True)
        self.db = db
        self.server_id = server_id
        self.bus = bus
        self.sched = Schedule()
        self.open_issue: int | None = None
        self.active = True
        self.sync: ServerSync | None = None
        self._commands: queue.Queue[tuple[str, tuple[Any, ...]]] = queue.Queue()
        self._stopping = threading.Event()

    # --- 外部からの依頼(どのスレッドから呼んでもよい) --------------------------
    def send(self, command: str, *args: Any) -> None:
        self._commands.put((command, args))

    def stop(self) -> None:
        self._stopping.set()
        self._commands.put(("noop", ()))

    # --- スレッド本体 ----------------------------------------------------------
    def run(self) -> None:
        try:
            if not self._setup():
                return
            next_tick = 0.0
            while not self._stopping.is_set():
                timeout = max(0.0, next_tick - time.monotonic())
                try:
                    cmd, args = self._commands.get(timeout=timeout)
                    self._handle(cmd, args)
                    continue  # 続けて届いた依頼を先に処理する
                except queue.Empty:
                    pass
                self.tick()
                next_tick = time.monotonic() + TICK_SECONDS
        finally:
            if self.sync is not None:
                self.sync.client.close()

    def _setup(self) -> bool:
        server = ServerStore(self.db).get(self.server_id)
        key = get_api_key(self.server_id)
        if server is None or not key:
            log.warning("server %s has no API key; worker not started", self.server_id)
            return False
        client = RedmineClient(server.url, key, proxy_mode=server.proxy_mode, proxy_url=server.proxy_url)
        self.sync = ServerSync(self.db, self.server_id, client, self._retention())
        last_delete = Settings(self.db).get(f"last_delete_check:{self.server_id}") or 0
        now = time.monotonic()
        wall_elapsed = time.time() - float(last_delete)
        self.sched.delete_check = now + max(600.0, DELETE_CHECK_INTERVAL - wall_elapsed)
        self.sched.reconcile = now + 300
        return True

    def _handle(self, cmd: str, args: tuple[Any, ...]) -> None:
        if cmd == "sync_now":
            self.sched.changes = 0
            self.sched.backoff_until = 0
            self.sched.failures = 0
        elif cmd == "open":
            (issue_id,) = args
            self.open_issue = issue_id
            self.sched.open_issue = 0
            if issue_id is not None and self.sync is not None \
                    and not self.sync.issues.journals_synced(self.server_id, issue_id):
                self._enqueue(issue_id, front=True)
        elif cmd == "active":
            (self.active,) = args
        elif cmd == "attachment":
            (aid,) = args
            if aid not in self.sched.attachments:
                self.sched.attachments.append(aid)
        elif cmd == "post":
            self._post_note(*args)

    # --- コメント投稿 ------------------------------------------------------------
    def _post_note(self, issue_id: int, notes: str, private: bool) -> None:
        assert self.sync is not None
        issues = self.sync.issues
        try:
            self.sync.client.add_note(issue_id, notes, private)
        except RedmineError as e:
            log.warning("posting a note to #%s failed: %s", issue_id, e)
            # 送信に失敗したら、下書きとして残して再送できるようにする(設計書 §6)
            issues.save_draft(self.server_id, issue_id, notes, private, failed=True, error=e.code.value)
            self.bus.publish("noteFinished", serverId=self.server_id, issueId=issue_id,
                             error=e.code.value, detail=e.detail)
            return
        issues.clear_draft(self.server_id, issue_id)
        self.bus.publish("noteFinished", serverId=self.server_id, issueId=issue_id, error=None, detail="")
        try:
            r = self.sync.fetch_journals(issue_id, notify=False)
            self.sync.issues.mark_read(self.server_id, issue_id)
            self._emit(r, extra={issue_id})
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

    def tick(self) -> None:
        if self.sync is None:
            return
        now = time.monotonic()
        if now < self.sched.backoff_until:
            return
        try:
            self._run_due(now)
        except RedmineError as e:
            self._on_error(e)
        except LookupError:
            self._stopping.set()  # サーバーが削除された
        except Exception:  # noqa: BLE001 - 予期しないエラーでもワーカーは止めない
            log.exception("unexpected error in sync worker %s", self.server_id)
            self._on_error(RedmineError(ErrorCode.INVALID_RESPONSE))

    def _emit(self, result: SyncResult, extra: set[int] | None = None) -> None:
        ids = result.changed_issue_ids | result.unmonitored_issue_ids | result.deleted_issue_ids | (extra or set())
        if ids:
            self.bus.publish("dataChanged", serverId=self.server_id, issueIds=sorted(ids))
        if result.new_comments:
            self.bus.publish("newComments", serverId=self.server_id, comments=[c.__dict__ for c in result.new_comments])

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
            self.bus.publish("dataChanged", serverId=self.server_id, issueIds=[])
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

        # 1回につき journal 1件・添付1件だけ処理する(サーバーを叩きすぎない)
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

        def ready(ok: bool) -> None:
            self.bus.publish("attachmentReady", serverId=self.server_id, attachmentId=attachment_id, ok=ok)

        if a is None or not a.content_url:
            ready(False)
            return
        if a.local_path and Path(a.local_path).exists():
            ready(True)
            return
        ext = mimetypes.guess_extension(a.content_type or "") or ""
        dest = attachment_cache_dir(self.server_id) / f"{attachment_id}{ext}"
        try:
            sync.client.download(a.content_url, dest, MAX_ATTACHMENT_BYTES)
        except RedmineError as e:
            if e.code == ErrorCode.TOO_LARGE or not e.retryable:
                ready(False)
                return
            raise
        store.set_attachment_cached(self.server_id, attachment_id, str(dest))
        prune_attachment_cache(self.db)
        ready(True)

    # --- 成功・失敗 -------------------------------------------------------------
    def _on_success(self) -> None:
        servers = ServerStore(self.db)
        server = servers.get(self.server_id)
        if server is None:
            return
        if server.last_error is not None:
            log.info("server %s recovered", self.server_id)
        servers.update_fields(self.server_id, last_error=None, last_synced_at=utcnow())
        self.sched.failures = 0
        self.bus.publish("syncState", serverId=self.server_id)

    def _on_error(self, err: RedmineError) -> None:
        self.sched.failures += 1
        if err.retryable:
            delay = min(BACKOFF_MAX, BACKOFF_BASE * 2 ** (self.sched.failures - 1))
        else:
            delay = BACKOFF_MAX  # キーの誤りなどはすぐには直らないので、長めに待つ
        self.sched.backoff_until = time.monotonic() + delay
        log.warning("sync error on server %s: %s; retrying in %ss", self.server_id, err, delay)
        ServerStore(self.db).update_fields(self.server_id, last_error=err.code.value)
        self.bus.publish("syncState", serverId=self.server_id)


def prune_attachment_cache(db: Database, limit_mb: int | None = None) -> None:
    """画像キャッシュの合計が上限を超えたら、古いものから削除する(設計書 §5.3)。"""
    limit = (limit_mb if limit_mb is not None else int(Settings(db).get("image_cache_limit_mb"))) * 1024 * 1024
    store = IssueStore(db)
    sizes = []
    for sid, aid, path in store.cached_attachments_oldest_first():
        p = Path(path)
        sizes.append((sid, aid, p, p.stat().st_size if p.exists() else 0))
    total = sum(s[3] for s in sizes)
    for sid, aid, p, size in sizes:
        if total <= limit:
            break
        p.unlink(missing_ok=True)
        store.set_attachment_cached(sid, aid, None)
        total -= size


class SyncManager:
    """有効なサーバーごとに ServerWorker を動かす。"""

    def __init__(self, db: Database, bus: EventBus) -> None:
        self.db = db
        self.bus = bus
        self._lock = threading.Lock()
        self._workers: dict[int, ServerWorker] = {}
        self._active = True
        self._open: tuple[int | None, int | None] = (None, None)

    def restart(self) -> None:
        self.stop_all()
        with self._lock:
            for server in ServerStore(self.db).list(enabled_only=True):
                w = ServerWorker(self.db, server.server_id, self.bus)
                w.active = self._active
                self._workers[server.server_id] = w
                w.start()
            sid, iid = self._open
        if sid is not None:
            self.set_open_issue(sid, iid)

    def stop_all(self, timeout: float = 5.0) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for w in workers:
            w.stop()
        for w in workers:
            w.join(timeout)
            if w.is_alive():
                # 通信中で止まらない場合は待たずに進める(タイムアウトで必ず終わる。デーモンスレッド)
                log.warning("sync thread %s did not stop in time", w.name)

    def _worker(self, server_id: int) -> ServerWorker | None:
        with self._lock:
            return self._workers.get(server_id)

    def sync_now(self) -> None:
        with self._lock:
            for w in self._workers.values():
                w.send("sync_now")

    def set_active(self, active: bool) -> None:
        """ウィンドウが非アクティブの間はポーリング間隔を延ばす(設計書 §4.6)。"""
        with self._lock:
            self._active = active
            for w in self._workers.values():
                w.send("active", active)

    def set_open_issue(self, server_id: int | None, issue_id: int | None) -> None:
        with self._lock:
            self._open = (server_id, issue_id)
            for sid, w in self._workers.items():
                w.send("open", issue_id if sid == server_id else None)

    def request_attachment(self, server_id: int, attachment_id: int) -> bool:
        w = self._worker(server_id)
        if w is None:
            return False
        w.send("attachment", attachment_id)
        return True

    def post_note(self, server_id: int, issue_id: int, notes: str, private: bool) -> bool:
        w = self._worker(server_id)
        if w is None:
            return False
        w.send("post", issue_id, notes, private)
        return True
