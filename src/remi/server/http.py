"""画面とバックエンドをつなぐローカルHTTPサーバー(設計書 §2.6)。

- 127.0.0.1 のランダムなポートだけで待ち受ける
- 起動ごとのトークンをCookieに置き換えて認証する。Hostヘッダーも検査する(DNSリバインディング対策)
- GET  /                 画面(静的ファイル)
- POST /api/<method>     RPC(JSON)
- GET  /events           SSE
- GET  /attachments/<server_id>/<attachment_id>   キャッシュした添付(未取得なら取得を待つ)
- GET  /external-image?url=...                     外部画像(設定で許可した場合のみ)
- GET  /highlight.css    コードハイライトのCSS(ライト/ダーク)
"""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import secrets
import threading
import time
from collections import OrderedDict
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from remi import user_agent
from remi.render.markup import highlight_css
from remi.server import external_image
from remi.server.api import UPLOAD_MAX, RemiApi, RpcError

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
COOKIE = "remi_session"
ATTACHMENT_WAIT = 20.0
PENDING_KEEP = 50
PENDING_MAX_TOTAL = 100 * 1024 * 1024  # 送信前の画像(プレビュー用)としてメモリに置く合計の上限
EXTERNAL_IMAGE_MAX = 10 * 1024 * 1024
EXTERNAL_CACHE_KEEP = 50
EXTERNAL_CACHE_MAX_TOTAL = 50 * 1024 * 1024  # 外部画像のキャッシュの合計の上限
SSE_KEEPALIVE = 15.0

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


class LocalServer:
    def __init__(self, api: RemiApi, port: int = 0, web_dir: Path = WEB_DIR) -> None:
        self.api = api
        self.web_dir = web_dir.resolve()
        self.token = secrets.token_urlsafe(32)
        self.session = secrets.token_urlsafe(32)
        self._methods = api.methods()
        self._external_cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
        self._external_bytes = 0
        self._attachment_events: dict[tuple[int, int], threading.Event] = {}
        # 送信前の添付(プレビューで画像を表示する用。画像だけ)。id -> (server_id, token, content_type, data)
        self._pending: OrderedDict[int, tuple[int, str, str, bytes]] = OrderedDict()
        self._pending_bytes = 0
        self._pending_seq = 0
        self._lock = threading.Lock()
        api.ctx.bus.on(self._on_event)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def start_url(self) -> str:
        """最初に開くURL(トークン付き)。"""
        return f"{self.origin}/?token={self.token}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="local-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    def _drop_pending(self, pid: int) -> None:
        """self._lock を持って呼ぶ。"""
        got = self._pending.pop(pid, None)
        if got is not None:
            self._pending_bytes -= len(got[3])

    def _on_event(self, event: dict[str, Any]) -> None:
        if event.get("type") == "serverCacheCleared":
            with self._lock:
                for pid in [k for k, v in self._pending.items() if v[0] == event["serverId"]]:
                    self._drop_pending(pid)
        elif event.get("type") == "noteFinished" and not event.get("error") and event.get("uploadTokens"):
            # 送信できた添付は、もうプレビューに使わない
            tokens = set(event["uploadTokens"])
            with self._lock:
                for pid in [k for k, v in self._pending.items() if v[0] == event["serverId"] and v[1] in tokens]:
                    self._drop_pending(pid)
        elif event.get("type") == "attachmentReady":
            with self._lock:
                ev = self._attachment_events.get((event["serverId"], event["attachmentId"]))
            if ev is not None:
                ev.set()

    # --- 添付 -------------------------------------------------------------------
    def attachment_path(self, server_id: int, attachment_id: int) -> tuple[Path, str] | None:
        store = self.api.ctx.issues
        a = store.get_attachment(server_id, attachment_id)
        if a is None:
            return None
        if not (a.local_path and Path(a.local_path).exists()):
            if self.api.sync is None:
                return None
            key = (server_id, attachment_id)
            with self._lock:
                ev = self._attachment_events.setdefault(key, threading.Event())
            if not self.api.sync.request_attachment(server_id, attachment_id):
                return None
            ev.wait(ATTACHMENT_WAIT)
            with self._lock:
                self._attachment_events.pop(key, None)
            a = store.get_attachment(server_id, attachment_id)
            if a is None or not (a.local_path and Path(a.local_path).exists()):
                return None
        ctype = a.content_type or mimetypes.guess_type(a.filename)[0] or "application/octet-stream"
        return Path(a.local_path), ctype

    def upload(self, server_id: int, filename: str, data: bytes) -> dict[str, Any]:
        """Redmineへファイルをアップロードし、トークンを返す(設計書 §6.3)。"""
        from remi.api.errors import RedmineError

        client = self.api.sync.client(server_id) if self.api.sync is not None else None
        if client is None:
            raise RpcError("connection_failed")
        try:
            token = client.upload(data, filename)
        except RedmineError as e:
            raise RpcError(e.code.value, e.detail) from e
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        is_image = ctype.startswith("image/") and ctype != "image/svg+xml"
        with self._lock:
            self._pending_seq += 1
            pid = self._pending_seq
            if is_image and len(data) <= PENDING_MAX_TOTAL:  # 画像以外は表示しないので、メモリに置かない
                self._pending[pid] = (server_id, token, ctype, data)
                self._pending_bytes += len(data)
                while len(self._pending) > PENDING_KEEP or self._pending_bytes > PENDING_MAX_TOTAL:
                    self._drop_pending(next(iter(self._pending)))
        return {"id": pid, "token": token, "filename": filename, "contentType": ctype, "size": len(data),
                "isImage": is_image, "url": f"/pending/{pid}"}

    def external_image(self, url: str) -> tuple[bytes, str] | None:
        if not self.api.ctx.settings.get("allow_external_images"):
            return None
        if not url.lower().startswith(("http://", "https://")):
            return None
        with self._lock:
            if url in self._external_cache:
                self._external_cache.move_to_end(url)
                return self._external_cache[url]
        got = external_image.fetch(url, user_agent(), EXTERNAL_IMAGE_MAX)  # 接続先の検査もここで行う
        if got is None:
            return None
        with self._lock:
            if url not in self._external_cache and len(got[0]) <= EXTERNAL_CACHE_MAX_TOTAL:
                self._external_cache[url] = got
                self._external_bytes += len(got[0])
                while len(self._external_cache) > EXTERNAL_CACHE_KEEP or self._external_bytes > EXTERNAL_CACHE_MAX_TOTAL:
                    _, old = self._external_cache.popitem(last=False)
                    self._external_bytes -= len(old[0])
        return got

    # --- リクエスト処理 -------------------------------------------------------------
    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "Remi"
            sys_version = ""

            def log_message(self, fmt: str, *args: Any) -> None:
                log.debug("%s - " + fmt, self.address_string(), *args)

            # --- 共通 ---
            def _headers(self, status: int, ctype: str, length: int | None, extra: dict[str, str] | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                if length is not None:
                    self.send_header("Content-Length", str(length))
                self.send_header("Content-Security-Policy", CSP)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()

            def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
                self._headers(status, ctype, len(body), extra)
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, status: int, data: Any) -> None:
                self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

            def _deny(self, status: int = HTTPStatus.FORBIDDEN) -> None:
                self._send(status, b"", "text/plain")

            def _host_ok(self) -> bool:
                return self.headers.get("Host") in (f"127.0.0.1:{server.port}", f"localhost:{server.port}")

            def _authed(self) -> bool:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                morsel = cookie.get(COOKIE)
                return morsel is not None and hmac.compare_digest(morsel.value, server.session)

            # --- GET ---
            def do_GET(self) -> None:  # noqa: N802
                if not self._host_ok():
                    return self._deny()
                url = urlparse(self.path)
                query = parse_qs(url.query)
                if url.path == "/" and "token" in query:
                    if not hmac.compare_digest(query["token"][0], server.token):
                        return self._deny()
                    cookie = f"{COOKIE}={server.session}; HttpOnly; SameSite=Strict; Path=/"
                    return self._send(HTTPStatus.FOUND, b"", "text/plain",
                                      {"Location": "/", "Set-Cookie": cookie})
                if not self._authed():
                    return self._deny()
                if url.path == "/events":
                    return self._events()
                if url.path == "/highlight.css":
                    css = highlight_css("default", ".theme-light") + "\n" + highlight_css("github-dark", ".theme-dark")
                    return self._send(HTTPStatus.OK, css.encode(), "text/css; charset=utf-8")
                if url.path.startswith("/attachments/"):
                    return self._attachment(url.path)
                if url.path.startswith("/pending/"):
                    pid = url.path.rsplit("/", 1)[-1]
                    with server._lock:
                        got = server._pending.get(int(pid)) if pid.isdigit() else None
                    if got is None or not got[2].startswith("image/") or got[2] == "image/svg+xml":
                        return self._deny(HTTPStatus.NOT_FOUND)
                    return self._send(HTTPStatus.OK, got[3], got[2])
                if url.path == "/external-image":
                    got = server.external_image(query.get("url", [""])[0])
                    if got is None:
                        return self._deny(HTTPStatus.NOT_FOUND)
                    return self._send(HTTPStatus.OK, got[0], got[1])
                return self._static(url.path)

            do_HEAD = do_GET  # noqa: N815

            def _static(self, path: str) -> None:
                rel = "index.html" if path in ("/", "") else path.lstrip("/")
                target = (server.web_dir / rel).resolve()
                if server.web_dir not in target.parents and target != server.web_dir / "index.html":
                    return self._deny(HTTPStatus.NOT_FOUND)
                if not target.is_file():
                    return self._deny(HTTPStatus.NOT_FOUND)
                ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                    ctype += "; charset=utf-8"
                if target.suffix == ".js":
                    ctype = "text/javascript; charset=utf-8"
                self._send(HTTPStatus.OK, target.read_bytes(), ctype)

            def _attachment(self, path: str) -> None:
                parts = path.strip("/").split("/")
                if len(parts) != 3 or not (parts[1].isdigit() and parts[2].isdigit()):
                    return self._deny(HTTPStatus.NOT_FOUND)
                got = server.attachment_path(int(parts[1]), int(parts[2]))
                if got is None:
                    return self._deny(HTTPStatus.NOT_FOUND)
                file, ctype = got
                if ctype == "image/svg+xml":  # SVGはスクリプトを含みうるので表示しない(設計書 §5.3)
                    ctype = "application/octet-stream"
                self._send(HTTPStatus.OK, file.read_bytes(), ctype,
                           {"Content-Disposition": "inline" if ctype.startswith("image/") else "attachment"})

            def _events(self) -> None:
                bus = server.api.ctx.bus
                sub = bus.subscribe()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                last = time.monotonic()
                try:
                    self.wfile.write(b"retry: 2000\n\n")
                    self.wfile.flush()
                    while not server._stopping.is_set():
                        event = sub.get(timeout=1.0)
                        if event is not None:
                            data = json.dumps(event, ensure_ascii=False)
                            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                            self.wfile.flush()
                            last = time.monotonic()
                        elif time.monotonic() - last > SSE_KEEPALIVE:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            last = time.monotonic()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    bus.unsubscribe(sub)
                    self.close_connection = True

            # --- POST(RPC) ---
            def do_POST(self) -> None:  # noqa: N802
                # 接続を使い回すので、拒否する場合も先に本文を読み切っておく
                # 上限(UPLOAD_MAX)を超えるもの・長さがおかしいものは読まずに断る。
                # 実際の上限はRedmineの設定で決まる(超えたらRedmineが拒否する)
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length < 0:
                    self.close_connection = True
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": {"code": "bad_request"}})
                if length > UPLOAD_MAX:
                    self.close_connection = True
                    return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": {"code": "too_large"}})
                raw = self.rfile.read(length) if length else b""
                if not self._host_ok() or not self._authed():
                    return self._deny()
                url = urlparse(self.path)
                if url.path == "/upload":
                    return self._upload(url, raw)
                if not self.headers.get("Content-Type", "").startswith("application/json"):
                    return self._deny(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                if not url.path.startswith("/api/"):
                    return self._deny(HTTPStatus.NOT_FOUND)
                method = server._methods.get(url.path[len("/api/"):])
                if method is None:
                    return self._deny(HTTPStatus.NOT_FOUND)
                try:
                    params = json.loads(raw) if raw else {}
                    if not isinstance(params, dict):
                        raise ValueError
                except ValueError:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": {"code": "bad_request"}})
                try:
                    result = method(**params)
                except RpcError as e:
                    return self._json(HTTPStatus.OK, {"error": {"code": e.code, "detail": e.detail}})
                except TypeError as e:
                    log.warning("bad RPC call %s: %s", url.path, e)
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": {"code": "bad_request"}})
                except Exception:  # noqa: BLE001
                    log.exception("RPC %s failed", url.path)
                    return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"code": "internal"}})
                self._json(HTTPStatus.OK, {"result": result})

            def _upload(self, url, raw: bytes) -> None:
                # 別サイトからのフォーム送信を防ぐため、専用ヘッダーを必須にする(CSRF対策)
                if self.headers.get("X-Remi-Upload") != "1":
                    return self._deny()
                q = parse_qs(url.query)
                try:
                    server_id = int(q["serverId"][0])
                    filename = q["filename"][0].replace("/", "_").replace("\\", "_").strip()
                except (KeyError, ValueError, IndexError):
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": {"code": "bad_request"}})
                if not filename or not raw:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": {"code": "bad_request"}})
                try:
                    result = server.upload(server_id, filename, raw)
                except RpcError as e:
                    return self._json(HTTPStatus.OK, {"error": {"code": e.code, "detail": e.detail}})
                self._json(HTTPStatus.OK, {"result": result})

        return Handler
