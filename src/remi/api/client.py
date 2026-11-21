"""Redmine REST API クライアント(設計書 §2.4)。

- サーバーごとに1つ作り、requests.Session を使い回す
- タイムアウトを必ず指定する
- 同時に1リクエストだけ送り、リクエストの間に最小間隔を空ける(レート制御)
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlparse

import requests

from remi import user_agent
from remi.api.errors import ErrorCode, RedmineError

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = (5.0, 30.0)  # (接続, 読み取り) 秒
PAGE_LIMIT = 100
MAX_REDIRECTS = 5


def _origin(url: str) -> tuple[str, str, int]:
    """(スキーム, ホスト, ポート)。ホストは小文字、ポートは省略時の既定値で補う。"""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), parts.port or (443 if scheme == "https" else 80)


def same_server(base_url: str, url: str) -> bool:
    """APIキーを付けて送ってよい接続先か: 登録したRedmineと同じスキーム・ホスト・ポート。
    同じホストへの http → https の移動(より安全になる)も許す。"""
    try:
        b, u = _origin(base_url), _origin(url)
    except ValueError:
        return False
    if b == u:
        return True
    return b[1] == u[1] and b[0] == "http" and u[0] == "https" and u[2] == 443


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise RedmineError(ErrorCode.INVALID_URL)
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RedmineError(ErrorCode.INVALID_URL)
    return url.rstrip("/")


def format_redmine_datetime(value: str) -> str:
    """updated_on フィルタ用。'2026-11-17T10:00:00Z' の形にそろえる。"""
    return value.replace("+00:00", "Z")


# 通信の最小間隔(秒)。Redmineへの負荷を抑えるため、背景の同期は、リクエストの間をこれだけあける(設計書 §2.4)。
# テストでは待たないよう、conftest で 0 にする(間隔そのものは test_throttle で確かめる)
DEFAULT_MIN_INTERVAL = 0.5

_local = threading.local()


@contextlib.contextmanager
def interactive() -> Iterator[None]:
    """画面の操作(チケットを開く・コメントの送信・項目の変更など)のための通信にする(設計書 §2.4)。

    このスレッドからの通信は、最小間隔を空けずに送り、背景の同期に先んじる(背景の同期は、その間だけ譲る)。
    人が操作した分だけで、通信の量は増えない。一括で取得するもの(初回同期・バージョンのチケット)には使わない。
    """
    before = getattr(_local, "interactive", False)
    _local.interactive = True
    try:
        yield
    finally:
        _local.interactive = before


class RedmineClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        proxy_mode: str = "system",
        proxy_url: str | None = None,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
        min_interval: float | None = None,
    ) -> None:
        self.base_url = normalize_url(base_url)
        self.timeout = timeout
        self.min_interval = DEFAULT_MIN_INTERVAL if min_interval is None else min_interval
        self._lock = threading.Lock()  # 同時に送るのは1つだけ
        self._turn = threading.Condition()  # 背景の同期の順番待ち(画面の操作に譲る・最小間隔を空ける)
        self._interactive = 0  # 送信中・待機中の、画面の操作の通信の数
        self._last_request = 0.0
        # 同期を止めたあとは、通信中だったものも含めて結果を返さない(切り替えた後に前のデータを書き戻させない)
        self._cancelled = threading.Event()
        s = requests.Session()
        s.headers.update(
            {
                "X-Redmine-API-Key": api_key,
                "User-Agent": user_agent(),
                "Accept": "application/json",
            }
        )
        if proxy_mode == "manual" and proxy_url:
            s.trust_env = False
            s.proxies = {"http": proxy_url, "https": proxy_url}
        self.session = s

    def close(self) -> None:
        self.session.close()

    def cancel(self) -> None:
        """これ以降のリクエストを送らず、通信中のリクエストの結果も捨てる(CANCELLED)。どのスレッドから呼んでもよい。"""
        self._cancelled.set()
        with self._turn:
            self._turn.notify_all()  # 順番待ちをしている背景の同期を起こす

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    # --- 低レベル -----------------------------------------------------------
    def url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def _wait_for_turn(self) -> None:
        """背景の同期の順番待ち: 画面の操作の通信があるあいだは譲り、前の通信から最小間隔を空ける。

        待つのは送信の鍵の外で行う(鍵を持ったまま待つと、画面の操作まで待たされる)。
        """
        with self._turn:
            while True:
                if self.cancelled:
                    raise RedmineError(ErrorCode.CANCELLED)
                if self._interactive:
                    self._turn.wait()
                    continue
                wait = self._last_request + self.min_interval - time.monotonic()
                if wait <= 0:
                    return
                self._turn.wait(wait)

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """リクエストを送る。APIキーを付けるので、登録したRedmine以外へは送らない。

        requests のリダイレクトの自動追跡は、別のホストへ移るときに Authorization しか外さず、
        X-Redmine-API-Key はそのまま送ってしまう。そのため自動では追わず、移動先も検査してから追う。
        """
        kwargs.setdefault("timeout", self.timeout)
        kwargs["allow_redirects"] = False
        url = self.url(path)
        for _ in range(MAX_REDIRECTS + 1):
            if not same_server(self.base_url, url):
                log.warning("refused to send the API key to another host: %s", urlsplit(url).hostname)
                raise RedmineError(ErrorCode.OTHER_HOST, detail=urlsplit(url).hostname or "")
            resp = self._send(method, url, **kwargs)
            if not resp.is_redirect:
                break
            location = resp.headers.get("Location")
            resp.close()
            if not location or method not in ("GET", "HEAD"):
                raise RedmineError(ErrorCode.INVALID_RESPONSE, resp.status_code, "redirect")
            url = urljoin(url, location)
            kwargs.pop("params", None)  # 移動先のURLには、クエリが含まれている
        else:
            raise RedmineError(ErrorCode.INVALID_RESPONSE, resp.status_code, "too many redirects")
        log.debug("%s %s -> %s", method, path, resp.status_code)
        if resp.status_code >= 400:
            raise _error_for_status(resp)
        return resp

    def _send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        if self.cancelled:
            raise RedmineError(ErrorCode.CANCELLED)
        mine = getattr(_local, "interactive", False)
        if mine:
            with self._turn:
                self._interactive += 1
        else:
            self._wait_for_turn()
        try:
            resp = self._send_locked(method, url, **kwargs)
        finally:
            if mine:
                with self._turn:
                    self._interactive -= 1
                    self._turn.notify_all()
        if self.cancelled and method in ("GET", "HEAD"):
            # 取得した結果は捨てる(止めた後に書き込ませない)。変更を送った結果は、送れたかを伝えるために返す
            resp.close()
            raise RedmineError(ErrorCode.CANCELLED)
        return resp

    def _send_locked(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        with self._lock:
            try:
                resp = self.session.request(method, url, **kwargs)
            except requests.exceptions.SSLError as e:
                raise RedmineError(ErrorCode.SSL_ERROR, detail=str(e)) from e
            except requests.exceptions.ProxyError as e:
                raise RedmineError(ErrorCode.PROXY_ERROR, detail=str(e)) from e
            except requests.exceptions.Timeout as e:
                raise RedmineError(ErrorCode.TIMEOUT, detail=str(e)) from e
            except requests.exceptions.ConnectionError as e:
                raise RedmineError(ErrorCode.CONNECTION_FAILED, detail=str(e)) from e
            except requests.exceptions.InvalidURL as e:
                raise RedmineError(ErrorCode.INVALID_URL, detail=str(e)) from e
            except requests.exceptions.RequestException as e:
                # 応答の受信中に切れた(ChunkedEncodingError)など。一時的な通信の失敗として扱い、再試行させる
                raise RedmineError(ErrorCode.CONNECTION_FAILED, detail=str(e)) from e
            finally:
                self._last_request = time.monotonic()
        return resp

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self.request("GET", path, params=params)
        try:
            return resp.json()
        except (ValueError, json.JSONDecodeError) as e:
            raise RedmineError(ErrorCode.INVALID_RESPONSE, resp.status_code, "not json") from e

    def paged(self, path: str, key: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        """limit/offset でページングしながら1件ずつ返す。"""
        params = dict(params or {})
        offset = 0
        while True:
            params.update(limit=PAGE_LIMIT, offset=offset)
            data = self.get_json(path, params)
            items = data.get(key, [])
            yield from items
            total = data.get("total_count")
            offset += len(items)
            if not items or total is None or offset >= total:
                break

    # --- API -----------------------------------------------------------------
    def current_user(self) -> dict[str, Any]:
        return self.get_json("/users/current.json", {"include": "memberships,groups"})["user"]

    def projects(self) -> list[dict[str, Any]]:
        return list(self.paged("/projects.json", "projects"))

    def issue_statuses(self) -> list[dict[str, Any]]:
        return self.get_json("/issue_statuses.json").get("issue_statuses", [])

    def trackers(self) -> list[dict[str, Any]]:
        return self.get_json("/trackers.json").get("trackers", [])

    def issue_priorities(self) -> list[dict[str, Any]]:
        return self.get_json("/enumerations/issue_priorities.json").get("issue_priorities", [])

    def issues(self, **filters: Any) -> list[dict[str, Any]]:
        return list(self.paged("/issues.json", "issues", filters))

    def project(self, project_id: int, include: str | None = None) -> dict[str, Any]:
        params = {"include": include} if include else None
        return self.get_json(f"/projects/{project_id}.json", params)["project"]

    def custom_fields(self) -> list[dict[str, Any]]:
        """カスタムフィールドの定義(形式・選択肢)。管理者だけが取得できる(ほかは 403)。"""
        return self.get_json("/custom_fields.json").get("custom_fields", [])

    def versions(self, project_id: int) -> list[dict[str, Any]]:
        """プロジェクトのバージョン(ほかのプロジェクトから共有されたものを含む)。ページングはない。"""
        return self.get_json(f"/projects/{project_id}/versions.json").get("versions", [])

    def version(self, version_id: int) -> dict[str, Any]:
        return self.get_json(f"/versions/{version_id}.json")["version"]

    def issue_category(self, category_id: int) -> dict[str, Any]:
        return self.get_json(f"/issue_categories/{category_id}.json")["issue_category"]

    def user(self, user_id: int) -> dict[str, Any]:
        """ユーザー。見えないユーザー(権限による)とグループは 404 になる。"""
        return self.get_json(f"/users/{user_id}.json")["user"]

    def issues_signature(self, **filters: Any) -> str:
        """条件に合うチケットの「件数と最新の更新日時」。一覧が変わったかを1回の軽い問い合わせで判定する。

        チケットの追加・状態の変更は、そのチケットの updated_on を進める。外れた・削除された・見えなくなったものは
        件数を減らす(同時に追加があれば、追加したチケットの updated_on が進む)。
        """
        data = self.get_json("/issues.json", {**filters, "limit": 1, "sort": "updated_on:desc"})
        items = data.get("issues") or []
        latest = items[0].get("updated_on") if items else ""
        return f"{int(data.get('total_count', len(items)))}|{latest or ''}"

    def issue_count(self, **filters: Any) -> int:
        data = self.get_json("/issues.json", {**filters, "limit": 1})
        return int(data.get("total_count", 0))

    def issue(self, issue_id: int, include: str | None = None) -> dict[str, Any]:
        params = {"include": include} if include else None
        return self.get_json(f"/issues/{issue_id}.json", params)["issue"]

    def add_note(self, issue_id: int, notes: str, private: bool = False) -> None:
        self.update_issue(issue_id, notes=notes, private=private)

    # 変更できる項目(設計書 §6.2)。カスタムフィールドは "cf:<ID>" で渡す
    UPDATABLE = ("status_id", "assigned_to_id", "priority_id", "done_ratio", "tracker_id", "category_id",
                 "fixed_version_id", "parent_issue_id", "start_date", "due_date", "estimated_hours")

    def update_issue(self, issue_id: int, *, notes: str = "", private: bool = False,
                     changes: dict[str, Any] | None = None, uploads: list[dict[str, Any]] | None = None) -> None:
        """コメント・項目の変更・添付をまとめて1回で送る(Redmineの編集フォームと同じ)。"""
        issue: dict[str, Any] = {}
        if notes:
            issue["notes"] = notes
            issue["private_notes"] = bool(private)
        custom = []
        for k, v in (changes or {}).items():
            if k.startswith("cf:") and k[3:].isdigit():
                custom.append({"id": int(k[3:]), "value": "" if v is None else v})
                continue
            if k not in self.UPDATABLE:
                raise ValueError(k)
            issue[k] = "" if v is None else v  # 担当者などを外すときは空文字を送る
        if custom:
            issue["custom_fields"] = custom
        if uploads:
            issue["uploads"] = [
                {"token": u["token"], "filename": u["filename"], "content_type": u.get("content_type")}
                for u in uploads
            ]
        self.request("PUT", f"/issues/{issue_id}.json", json={"issue": issue})

    def update_journal(self, journal_id: int, notes: str, private: bool | None = None) -> None:
        """コメント(journal の notes)を書き換える(Redmine 5.0 以降)。notes を空にすると、項目の変更がない
        journal は削除される(変更があれば、本文だけが消える)。"""
        journal: dict[str, Any] = {"notes": notes}
        if private is not None:
            journal["private_notes"] = bool(private)
        self.request("PUT", f"/journals/{journal_id}.json", json={"journal": journal})

    def delete_attachment(self, attachment_id: int) -> None:
        self.request("DELETE", f"/attachments/{attachment_id}.json")

    def upload(self, data: bytes, filename: str) -> str:
        """ファイルをアップロードし、添付に使うトークンを返す。"""
        resp = self.request("POST", "/uploads.json", params={"filename": filename}, data=data,
                            headers={"Content-Type": "application/octet-stream"})
        try:
            return resp.json()["upload"]["token"]
        except (ValueError, KeyError) as e:
            raise RedmineError(ErrorCode.INVALID_RESPONSE, resp.status_code, "no upload token") from e

    def allowed_statuses(self, issue_id: int) -> list[dict[str, Any]] | None:
        """ワークフロー上、遷移できるステータス(Redmine 5.0 以降)。取得できなければ None。"""
        data = self.issue(issue_id, include="allowed_statuses")
        return data.get("allowed_statuses")

    def role(self, role_id: int) -> dict[str, Any]:
        """ロールと、その権限(permissions)。管理者でなくてもAPIからは読める。"""
        return self.get_json(f"/roles/{role_id}.json")["role"]

    def memberships(self, project_id: int) -> list[dict[str, Any]]:
        return list(self.paged(f"/projects/{project_id}/memberships.json", "memberships"))

    def add_watcher(self, issue_id: int, user_id: int) -> None:
        self.request("POST", f"/issues/{issue_id}/watchers.json", json={"user_id": user_id})

    def remove_watcher(self, issue_id: int, user_id: int) -> None:
        self.request("DELETE", f"/issues/{issue_id}/watchers/{user_id}.json")

    def attachment_url(self, attachment_id: int, filename: str) -> str:
        """添付のダウンロード先。Redmineが返す content_url はRedmineの設定(ホスト名・プロトコル)から作られ、
        接続先と違うことがある(http になっている・別名のホスト)ので、登録したURLから組み立てる。"""
        return self.url(f"/attachments/download/{int(attachment_id)}/{quote(filename or 'file', safe='')}")

    def download(self, url: str, dest: Path, max_bytes: int) -> int:
        """添付ファイルをダウンロードする。上限を超えたら TOO_LARGE。"""
        resp = self.request("GET", url, stream=True)
        size = int(resp.headers.get("Content-Length") or 0)
        if size and size > max_bytes:
            resp.close()
            raise RedmineError(ErrorCode.TOO_LARGE, resp.status_code)
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            with open(tmp, "wb") as f:
                try:
                    for chunk in resp.iter_content(64 * 1024):
                        if self.cancelled:
                            raise RedmineError(ErrorCode.CANCELLED)
                        written += len(chunk)
                        if written > max_bytes:
                            raise RedmineError(ErrorCode.TOO_LARGE, resp.status_code)
                        f.write(chunk)
                except requests.exceptions.RequestException as e:
                    # 受信の途中で切れた・タイムアウトした。一時的な失敗として再試行させる
                    raise RedmineError(ErrorCode.CONNECTION_FAILED, resp.status_code, str(e)) from e
            tmp.replace(dest)
        finally:
            resp.close()
            tmp.unlink(missing_ok=True)
        return written


RETRY_AFTER_MAX = 3600.0  # Retry-After に従って待つ上限(秒)。異常に長い指定(何日も止まる)から守るための上限で、これを超える指定は1時間に丸める


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After ヘッダー(秒数、またはHTTPの日時)を、待つ秒数にする。読めなければ None。"""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    return min(max(seconds, 0.0), RETRY_AFTER_MAX)


def _error_for_status(resp: requests.Response) -> RedmineError:
    status = resp.status_code
    detail = ""
    try:
        errors = resp.json().get("errors")
        if errors:
            detail = "; ".join(map(str, errors))
    except ValueError:
        pass
    if status == 401:
        return RedmineError(ErrorCode.UNAUTHORIZED, status, detail)
    if status == 403:
        return RedmineError(ErrorCode.FORBIDDEN, status, detail)
    if status == 404:
        return RedmineError(ErrorCode.NOT_FOUND, status, detail)
    if status == 422:
        return RedmineError(ErrorCode.UNPROCESSABLE, status, detail)
    retry_after = parse_retry_after(resp.headers.get("Retry-After"))
    if status == 429:
        return RedmineError(ErrorCode.RATE_LIMITED, status, detail, retry_after=retry_after)
    if status >= 500:
        return RedmineError(ErrorCode.SERVER_ERROR, status, detail, retry_after=retry_after)
    return RedmineError(ErrorCode.INVALID_RESPONSE, status, detail)
