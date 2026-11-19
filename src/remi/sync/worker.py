"""サーバーごとの同期ワーカー(スレッド)と、それらを束ねる SyncManager(設計書 §2.1, §4.6)。

- サーバーごとに独立したスレッドで動き、1台が落ちても他に影響しない
- 約1秒ごとに、期限が来た処理だけを実行する
- 画面などからの依頼はコマンドキューで受け取り、ワーカースレッドで実行する
- エラーが起きたら指数バックオフする(サーバー別)。サーバーが Retry-After を返したら、それ以上待つ
- 結果はイベントバスへ流す

多数の利用者が同じRedmineを使っても叩きすぎないよう、次のようにする:
- 各処理の間隔とバックオフにジッター(ばらつき)を入れ、起動・回復のタイミングがそろわないようにする
- アプリの起動時は、最初の同期を少しずらす(朝に一斉に起動しても集中しない)
- プロジェクト一覧・マスタは数時間ごと。前回の取得時刻を保存し、起動のたびには取り直さない
- 画面が見えていない(トレイに格納・最小化)間は、開いているチケットの確認を止める
- コメントの遅延取得は数秒に1件にする。「今すぐ同期」には最短間隔を設ける
"""

from __future__ import annotations

import logging
import mimetypes
import queue
import random
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
from remi.sync.engine import ServerDeletedError, ServerSync, SyncResult

log = logging.getLogger(__name__)

TICK_SECONDS = 1.0
OPEN_ISSUE_INTERVAL = 20  # 秒
IDENTITY_INTERVAL = 3600
METADATA_INTERVAL = 6 * 3600  # プロジェクト一覧・マスタ。めったに変わらないので間隔を空ける
MANUAL_METADATA_MIN_AGE = 900  # 「今すぐ同期」では、これより古ければプロジェクト一覧・マスタも取り直す
RECONCILE_INTERVAL = 3600
VERSIONS_MIN_INTERVAL = 300  # バージョンの監視の最短間隔(秒)。チケットの差分同期より軽く回す
DELETE_CHECK_INTERVAL = 24 * 3600
INACTIVE_FACTOR = 2
BACKOFF_BASE = 30
BACKOFF_MAX = 1800
JITTER = 0.15  # 間隔のばらつき(±15%)
STARTUP_DELAY_MAX = 60.0  # アプリの起動時に最初の同期をずらす最大の秒数(SyncManager.restart(startup=True))
JOURNAL_QUEUE_INTERVAL = 3.0  # コメントの遅延取得の間隔(秒)。開いているチケットは別に、すぐ取得する
SYNC_NOW_MIN_INTERVAL = 30.0  # 「今すぐ同期」の最短間隔(秒)。連打してもサーバーへは送らない
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
    versions: float = 0.0
    delete_check: float = 0.0
    backoff_until: float = 0.0
    failures: int = 0
    metadata_at: float = float("-inf")  # 前回プロジェクト一覧・マスタを取得した時刻
    journal_next: float = 0.0  # 次にコメントの遅延取得をしてよい時刻
    manual_sync_at: float = float("-inf")  # 前回の「今すぐ同期」
    queue: list[int] = field(default_factory=list)  # journal を取得するチケット
    attachments: list[int] = field(default_factory=list)


def jittered(seconds: float) -> float:
    """間隔にばらつきを入れる。多数のクライアントの問い合わせが同じ時刻にそろわないようにする。"""
    return seconds * random.uniform(1 - JITTER, 1 + JITTER)


class ServerWorker(threading.Thread):
    def __init__(self, db: Database, server_id: int, bus: EventBus, start_delay: float = 0.0) -> None:
        super().__init__(name=f"sync-{server_id}", daemon=True)
        self.db = db
        self.server_id = server_id
        self.bus = bus
        self.sched = Schedule()
        self.open_issue: int | None = None
        # 開いた直後の1回目は、更新日時が同じでもコメントごと取り直す。updated_on は秒単位なので、
        # 同じ秒に入った更新を見逃していても、開けば(F5で開き直しても)必ず最新になるようにする
        self.open_issue_full = False
        self.active = True
        self.visible = True  # 画面が見えているか(トレイに格納・最小化していない)
        self.start_delay = start_delay  # 最初の同期をずらす最大の秒数
        self.sync: ServerSync | None = None
        self._commands: queue.Queue[tuple[str, tuple[Any, ...]]] = queue.Queue()
        self._stopping = threading.Event()

    # --- 外部からの依頼(どのスレッドから呼んでもよい) --------------------------
    def send(self, command: str, *args: Any) -> None:
        self._commands.put((command, args))

    def stop(self) -> None:
        """止める。通信中のリクエストの結果も捨てさせるので、止めた後にDBへ書き込むことはない
        (書き込みの途中だった1件を除く。stop_all はそれを join で待つ)。"""
        self._stopping.set()
        if self.sync is not None:
            self.sync.client.cancel()
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
                except queue.Empty:
                    pass
                else:
                    try:
                        self._dispatch(cmd, args)
                    except ServerDeletedError:
                        self._stopping.set()
                    except Exception:  # noqa: BLE001 - 予期しないエラーでもワーカーは止めない
                        log.exception("unexpected error in sync worker %s (%s)", self.server_id, cmd)
                    if cmd in ("sync_now", "sync_versions"):
                        next_tick = 0.0  # 次の刻みを待たず、すぐ同期を始める
                    continue  # 続けて届いた依頼を先に処理する
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
        if self._stopping.is_set():
            client.cancel()  # 作る前に stop() が呼ばれていた
        settings = Settings(self.db)
        last_delete = settings.get(f"last_delete_check:{self.server_id}") or 0
        now = time.monotonic()
        wall_elapsed = time.time() - float(last_delete)
        self.sched.delete_check = now + max(600.0, DELETE_CHECK_INTERVAL - wall_elapsed)
        self.sched.reconcile = now + jittered(300)
        # プロジェクト一覧・マスタは、前回の取得から METADATA_INTERVAL 経つまで取り直さない(起動のたびには取らない)。
        # まだ持っていない(初めて・キャッシュを消した)ときは、すぐ取得する
        if self._has_metadata():
            meta_age = max(0.0, time.time() - float(settings.get(f"last_metadata:{self.server_id}") or 0))
            self.sched.metadata_at = now - meta_age
            self.sched.metadata = now + max(0.0, METADATA_INTERVAL - meta_age)
        # アプリの起動時は、最初の同期を少しずらす(初回同期がまだなら、待たせない)
        if self.start_delay > 0 and server.initial_sync_done:
            first = now + random.uniform(0, self.start_delay)
            self.sched.identity = self.sched.changes = self.sched.versions = first
            self.sched.metadata = max(self.sched.metadata, first)
        return True

    def _has_metadata(self) -> bool:
        return self.db.query_one("SELECT 1 FROM masters WHERE server_id = ? AND kind = 'status' LIMIT 1",
                                 (self.server_id,)) is not None

    # threading.Thread は内部で _handle などの属性を使う(Python 3.13 では _handle を上書きする)ので、別の名前にする
    def _dispatch(self, cmd: str, args: tuple[Any, ...]) -> None:
        if self._stopping.is_set():
            return
        if cmd == "sync_now":
            (manual,) = args or (True,)
            now = time.monotonic()
            if manual:
                if now - self.sched.manual_sync_at < SYNC_NOW_MIN_INTERVAL:
                    return  # 連打してもサーバーへは送らない
                self.sched.manual_sync_at = now
                if now - self.sched.metadata_at >= MANUAL_METADATA_MIN_AGE:
                    self.sched.metadata = 0
            self.sched.changes = 0
            self.sched.versions = 0
            # バックオフ中でも1回は試す(VPNをつなぎ直したときなど)。失敗の回数は残すので、また失敗すれば
            # 前より長く待つ。連打は上の最短間隔で抑える
            self.sched.backoff_until = 0
        elif cmd == "sync_versions":
            # プロジェクトの設定を変えたときなど: バージョンの監視だけをすぐ確かめる(チケットの差分同期は要らない)
            self.sched.versions = 0
            self.sched.backoff_until = 0
            self.bus.set_activity(self.server_id, "versions", True)
        elif cmd == "open":
            (issue_id,) = args
            self.open_issue = issue_id
            self.open_issue_full = issue_id is not None
            self.sched.open_issue = 0
            if issue_id in self.sched.queue:
                self.sched.queue.remove(issue_id)  # すぐ下の tick で取得する
        elif cmd == "active":
            (self.active,) = args
        elif cmd == "visible":
            (visible,) = args
            if visible and not self.visible:
                self.sched.open_issue = 0  # 見えなかった間の変化を、すぐ確かめる
            self.visible = visible
        elif cmd == "attachment":
            (aid,) = args
            if aid not in self.sched.attachments:
                self.sched.attachments.append(aid)
        elif cmd == "update":
            self._update_issue(*args)
        elif cmd == "refetch":
            (issue_id,) = args
            try:
                self._emit(self.sync.fetch_journals(issue_id, notify=False), extra={issue_id})
            except RedmineError as e:
                log.info("refetch #%s failed: %s", issue_id, e)
        elif cmd == "watch":
            self._set_watching(*args)
        elif cmd == "version":
            (version_id,) = args
            try:
                changed = self.sync.sync_version(version_id)
            except RedmineError as e:
                log.info("fetching version %s failed: %s", version_id, e)
                self.bus.publish("versionFetchFailed", serverId=self.server_id, versionId=version_id,
                                 error=e.code.value)
                return
            if changed:
                self.bus.publish("versionsChanged", serverId=self.server_id, versionIds=[version_id])

    # --- コメント投稿・項目の変更・ウォッチ ------------------------------------------
    def _update_issue(self, issue_id: int, notes: str, private: bool, changes: dict | None,
                      uploads: list | None) -> None:
        assert self.sync is not None
        issues = self.sync.issues
        try:
            self.sync.client.update_issue(issue_id, notes=notes, private=private, changes=changes, uploads=uploads)
        except RedmineError as e:
            if e.code == ErrorCode.CANCELLED:
                # 送る前に同期を止めた(サーバーの設定を保存した)。下書きとして残し、再送できるようにする
                if notes:
                    issues.save_draft(self.server_id, issue_id, notes, private, failed=True, error=e.code.value)
                self.bus.publish("noteFinished", serverId=self.server_id, issueId=issue_id,
                                 error=e.code.value, detail="")
                return
            log.warning("updating #%s failed: %s", issue_id, e)
            # 送信に失敗したら、下書きとして残して再送できるようにする(設計書 §6)
            if notes:
                issues.save_draft(self.server_id, issue_id, notes, private, failed=True, error=e.code.value)
            self.bus.publish("noteFinished", serverId=self.server_id, issueId=issue_id,
                             error=e.code.value, detail=e.detail)
            return
        issues.clear_draft(self.server_id, issue_id)
        self.bus.publish("noteFinished", serverId=self.server_id, issueId=issue_id, error=None, detail="",
                         uploadTokens=[u["token"] for u in uploads or []])
        try:
            r = self.sync.fetch_journals(issue_id, notify=False)
            issues.mark_read(self.server_id, issue_id)
            self._emit(r, extra={issue_id})
        except RedmineError as e:
            log.info("refetch after updating failed: %s", e)

    def _set_watching(self, issue_id: int, watching: bool) -> None:
        assert self.sync is not None
        me = self.sync.server.my_user_id
        try:
            if watching:
                self.sync.client.add_watcher(issue_id, me)
            else:
                self.sync.client.remove_watcher(issue_id, me)
        except RedmineError as e:
            self.bus.publish("watchFinished", serverId=self.server_id, issueId=issue_id, error=e.code.value)
            return
        self.sync.set_watching_flag(issue_id, watching)
        self.bus.publish("watchFinished", serverId=self.server_id, issueId=issue_id, error=None)
        self.bus.publish("dataChanged", serverId=self.server_id, issueIds=[issue_id])

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
            if e.code != ErrorCode.CANCELLED:
                self._on_error(e)
        except ServerDeletedError:
            # KeyError なども LookupError なので、サーバーの削除だけを専用の例外で見分ける
            # (想定外の応答で KeyError が起きたときに、黙って同期を止めない)
            self._stopping.set()
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
            self.sched.identity = now + jittered(IDENTITY_INTERVAL)
            did_network = True
        if now >= self.sched.metadata:
            sync.refresh_metadata()
            Settings(self.db).set(f"last_metadata:{self.server_id}", time.time())
            self.sched.metadata_at = now
            self.sched.metadata = now + jittered(METADATA_INTERVAL)
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
            self.sched.changes = now + jittered(self._interval(server.poll_interval))
            did_network = True
        if now >= self.sched.versions:
            # チケットを取り直したバージョンは、1つずつすぐ画面に反映する(最初の取得は時間がかかるため)
            notified: set[int] = set()

            def on_version(ids: set[int]) -> None:
                notified.update(ids)
                self.bus.publish("versionsChanged", serverId=self.server_id, versionIds=sorted(ids))

            # 同期の間だけ、画面に「ロードマップを取得しています」を出す
            self.bus.set_activity(self.server_id, "versions", True)
            try:
                rest = sync.sync_versions(on_changed=on_version) - notified
            finally:
                self.bus.set_activity(self.server_id, "versions", False)
            if rest:
                self.bus.publish("versionsChanged", serverId=self.server_id, versionIds=sorted(rest))
            self.sched.versions = now + jittered(self._interval(max(server.poll_interval, VERSIONS_MIN_INTERVAL)))
            did_network = True
        # 画面が見えていない間は確かめない(そのチケットの新着は、上の差分同期で見つかる)
        if self.open_issue is not None and self.visible and now >= self.sched.open_issue:
            if self.open_issue_full:
                self._emit(sync.fetch_journals(self.open_issue))
                self.open_issue_full = False  # 失敗したら(例外)、次の確認でもコメントごと取り直す
            else:
                self._emit(sync.check_issue(self.open_issue))
            self.sched.open_issue = now + jittered(self._interval(OPEN_ISSUE_INTERVAL))
            did_network = True
        if now >= self.sched.reconcile:
            self._emit(sync.reconcile())
            self.sched.reconcile = now + jittered(RECONCILE_INTERVAL)
            did_network = True
        if now >= self.sched.delete_check:
            self._emit(sync.detect_deleted())
            Settings(self.db).set(f"last_delete_check:{self.server_id}", time.time())
            self.sched.delete_check = now + jittered(DELETE_CHECK_INTERVAL)
            did_network = True

        # 1回につき journal 1件・添付1件だけ処理する。journal は JOURNAL_QUEUE_INTERVAL ごと(サーバーを叩きすぎない)
        if self.sched.queue and now >= self.sched.journal_next:
            iid = self.sched.queue.pop(0)
            self.sched.journal_next = now + JOURNAL_QUEUE_INTERVAL
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
            sync.client.download(sync.client.attachment_url(attachment_id, a.filename), dest, MAX_ATTACHMENT_BYTES)
        except RedmineError as e:
            if e.code == ErrorCode.TOO_LARGE or not e.retryable:
                ready(False)
                return
            if attachment_id not in self.sched.attachments:
                self.sched.attachments.append(attachment_id)  # 一時的な失敗: バックオフの後で取り直す
            raise
        store.set_attachment_cached(self.server_id, attachment_id, str(dest))
        prune_attachment_cache(self.db)
        ready(True)

    # --- 成功・失敗 -------------------------------------------------------------
    def _on_success(self) -> None:
        if self._stopping.is_set():
            return
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
        if self._stopping.is_set():
            return
        self.sched.failures += 1
        if err.retryable:
            delay = min(BACKOFF_MAX, BACKOFF_BASE * 2 ** (self.sched.failures - 1))
        else:
            delay = BACKOFF_MAX  # キーの誤りなどはすぐには直らないので、長めに待つ
        delay = jittered(delay)  # 障害から回復したとき、全員が同時に問い合わせないようにする
        if err.retry_after is not None:
            delay = max(delay, err.retry_after)  # サーバーが指定した時間は必ず待つ(流量制限)
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
        self._visible = True
        self._open: tuple[int | None, int | None] = (None, None)

    def restart(self, startup: bool = False) -> None:
        """同期スレッドを作り直す。startup(アプリの起動時)なら、最初の同期をサーバーごとにランダムにずらす。"""
        self.stop_all()
        with self._lock:
            for server in ServerStore(self.db).list(enabled_only=True):
                w = ServerWorker(self.db, server.server_id, self.bus,
                                 start_delay=STARTUP_DELAY_MAX if startup else 0.0)
                w.active = self._active
                w.visible = self._visible
                self._workers[server.server_id] = w
                w.start()
            sid, iid = self._open
        if sid is not None:
            self.set_open_issue(sid, iid)

    def stop_all(self, timeout: float = 5.0) -> bool:
        """すべての同期スレッドを止める。時間内に止まらないものがあれば False。

        止めると、次のリクエストは送らず、通信中のリクエストの結果も捨てる(RedmineClient.cancel)。
        そのため、ページングや複数の処理が残っていても、通信中の1件が終われば止まる。
        True を返したときはスレッドが終わっているので、この後にDBへ書き込まれることはない。
        """
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for w in workers:
            w.stop()
        deadline = time.monotonic() + timeout
        stopped = True
        for w in workers:
            w.join(max(0.0, deadline - time.monotonic()))
            if w.is_alive():
                log.warning("sync thread %s did not stop in time", w.name)
                stopped = False
        return stopped

    def _worker(self, server_id: int) -> ServerWorker | None:
        with self._lock:
            return self._workers.get(server_id)

    def sync_now(self, manual: bool = True) -> None:
        """今すぐ同期する。manual(利用者の操作)なら、最短間隔より短い連打は無視する。"""
        with self._lock:
            for w in self._workers.values():
                w.send("sync_now", manual)

    def sync_versions_now(self, server_id: int) -> None:
        """バージョンの監視だけを、すぐ同期する(プロジェクトの設定を変えたとき)。"""
        with self._lock:
            w = self._workers.get(server_id)
            if w is not None:
                w.send("sync_versions")

    def set_active(self, active: bool) -> None:
        """ウィンドウが非アクティブの間はポーリング間隔を延ばす(設計書 §4.6)。"""
        with self._lock:
            self._active = active
            for w in self._workers.values():
                w.send("active", active)

    def set_visible(self, visible: bool) -> None:
        """画面が見えていない(トレイに格納・最小化)間は、開いているチケットの確認を止める。"""
        with self._lock:
            self._visible = visible
            for w in self._workers.values():
                w.send("visible", visible)

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

    def post_note(self, server_id: int, issue_id: int, notes: str, private: bool,
                  changes: dict | None = None, uploads: list | None = None) -> bool:
        """コメント・項目の変更・添付をまとめて送る。結果は noteFinished イベントで届く。"""
        w = self._worker(server_id)
        if w is None:
            return False
        w.send("update", issue_id, notes, private, changes, uploads)
        return True

    def fetch_version(self, server_id: int, version_id: int) -> bool:
        """画面で開いたバージョンのチケットを取得する(終了したバージョンは自動では取得しないため)。
        変わっていれば versionsChanged、失敗したら versionFetchFailed が届く。"""
        w = self._worker(server_id)
        if w is None:
            return False
        w.send("version", version_id)
        return True

    def refetch_issue(self, server_id: int, issue_id: int) -> bool:
        """チケットをコメント・添付ごと取り直す(コメントの編集・添付の削除のあと)。"""
        w = self._worker(server_id)
        if w is None:
            return False
        w.send("refetch", issue_id)
        return True

    def set_watching(self, server_id: int, issue_id: int, watching: bool) -> bool:
        w = self._worker(server_id)
        if w is None:
            return False
        w.send("watch", issue_id, watching)
        return True

    def client(self, server_id: int) -> RedmineClient | None:
        """画面からの問い合わせ(編集の選択肢・アップロード)に使うクライアント。

        RedmineClient はリクエストを1本ずつ送るので、同期スレッドと共有してよい。
        """
        w = self._worker(server_id)
        return w.sync.client if w is not None and w.sync is not None else None
