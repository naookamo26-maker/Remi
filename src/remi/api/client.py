"""Redmine REST API クライアント(設計書 §2.4)。

- サーバーごとに1つ作り、requests.Session を使い回す
- タイムアウトを必ず指定する
- 同時に1リクエストだけ送り、リクエストの間に最小間隔を空ける(レート制御)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from remi import user_agent
from remi.api.errors import ErrorCode, RedmineError

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = (5.0, 30.0)  # (接続, 読み取り) 秒
PAGE_LIMIT = 100


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


class RedmineClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        proxy_mode: str = "system",
        proxy_url: str | None = None,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
        min_interval: float = 0.5,
    ) -> None:
        self.base_url = normalize_url(base_url)
        self.timeout = timeout
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last_request = 0.0
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

    # --- 低レベル -----------------------------------------------------------
    def url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def _throttle(self) -> None:
        wait = self._last_request + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        with self._lock:
            self._throttle()
            try:
                resp = self.session.request(method, self.url(path), **kwargs)
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
            finally:
                self._last_request = time.monotonic()
        log.debug("%s %s -> %s", method, path, resp.status_code)
        if resp.status_code >= 400:
            raise _error_for_status(resp)
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

    def issue_count(self, **filters: Any) -> int:
        data = self.get_json("/issues.json", {**filters, "limit": 1})
        return int(data.get("total_count", 0))

    def issue(self, issue_id: int, include: str | None = None) -> dict[str, Any]:
        params = {"include": include} if include else None
        return self.get_json(f"/issues/{issue_id}.json", params)["issue"]

    def add_note(self, issue_id: int, notes: str, private: bool = False) -> None:
        body = {"issue": {"notes": notes, "private_notes": bool(private)}}
        self.request("PUT", f"/issues/{issue_id}.json", json=body)

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
                for chunk in resp.iter_content(64 * 1024):
                    written += len(chunk)
                    if written > max_bytes:
                        raise RedmineError(ErrorCode.TOO_LARGE, resp.status_code)
                    f.write(chunk)
            tmp.replace(dest)
        finally:
            resp.close()
            tmp.unlink(missing_ok=True)
        return written


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
    if status >= 500:
        return RedmineError(ErrorCode.SERVER_ERROR, status, detail)
    return RedmineError(ErrorCode.INVALID_RESPONSE, status, detail)
