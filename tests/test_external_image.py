"""外部画像の取得: 接続先の検査(SSRF対策)。"""

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from remi.server import external_image
from remi.server.external_image import blocked_address, fetch, url_allowed

PNG = b"\x89PNG\r\n\x1a\nfake"


@pytest.mark.parametrize("address", [
    "127.0.0.1", "127.1.2.3", "::1", "::ffff:127.0.0.1", "0.0.0.0", "0.1.2.3", "::",
    "169.254.169.254", "fe80::1", "fe80::1%eth0", "224.0.0.1", "ff02::1", "240.0.0.1", "not-an-ip",
])
def test_blocked_addresses(address):
    assert blocked_address(address)


@pytest.mark.parametrize("address", ["10.0.0.5", "172.16.1.1", "192.168.1.10", "8.8.8.8", "2001:4860:4860::8888"])
def test_private_and_public_addresses_are_allowed(address):
    # プライベートIP(社内サーバー)は許可する
    assert not blocked_address(address)


def _fake_dns(monkeypatch, table):
    def getaddrinfo(host, port, *args, **kwargs):
        if host not in table:
            raise socket.gaierror("not found")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in table[host]]

    monkeypatch.setattr(external_image.socket, "getaddrinfo", getaddrinfo)


def test_url_allowed_checks_resolved_addresses(monkeypatch):
    _fake_dns(monkeypatch, {
        "intranet.example": ["10.0.0.5"], "public.example": ["93.184.216.34"],
        "evil.example": ["127.0.0.1"], "metadata.example": ["169.254.169.254"],
        "mixed.example": ["93.184.216.34", "127.0.0.1"],
    })
    assert url_allowed("http://intranet.example/a.png")
    assert url_allowed("https://public.example/a.png")
    # 内部のアドレスに名前解決されるホスト名は拒否する。1つでも含まれていたら拒否する
    assert not url_allowed("http://evil.example/a.png")
    assert not url_allowed("http://metadata.example/latest/meta-data/")
    assert not url_allowed("http://mixed.example/a.png")
    assert not url_allowed("http://unknown.example/a.png")
    for bad in ("ftp://public.example/a.png", "file:///etc/passwd", "http:///a.png", "http://public.example:99999/",
                "javascript:alert(1)"):
        assert not url_allowed(bad), bad


def test_url_allowed_with_real_resolver():
    # 表記の偽装(localhost、10進・16進表記、IPv6)も、名前解決の結果で拒否する
    for url in ("http://localhost/", "http://127.0.0.1/", "http://2130706433/", "http://0x7f000001/",
                "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://169.254.169.254/", "http://0.0.0.0/"):
        assert not url_allowed(url), url


class _ImageServer:
    """テスト用の画像サーバー。routes: パス → (ステータス, ヘッダー, 本文)。"""

    def __init__(self, host):
        self.routes = {}
        self.hits = []
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802
                srv.hits.append(self.path)
                status, headers, body = srv.routes.get(self.path, (404, {}, b""))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer((host, 0), Handler)
        self.url = f"http://{host}:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def servers(monkeypatch):
    """127.0.0.1 を「公開サーバー」、127.0.0.2 を「内部のサーバー」とみなして試す。"""
    try:
        internal = _ImageServer("127.0.0.2")
    except OSError:
        pytest.skip("127.0.0.2 is not available")
    public = _ImageServer("127.0.0.1")
    monkeypatch.setattr(external_image, "blocked_address", lambda a: a != "127.0.0.1")
    monkeypatch.setenv("NO_PROXY", "*")  # 環境のプロキシを使わない
    yield public, internal
    public.close()
    internal.close()


def test_fetch_public_image_and_redirects(servers):
    public, internal = servers
    public.routes["/a.png"] = (200, {"Content-Type": "image/png"}, PNG)
    public.routes["/moved"] = (302, {"Location": "/a.png"}, b"")
    public.routes["/to-internal"] = (302, {"Location": f"{internal.url}/secret.png"}, b"")
    public.routes["/loop"] = (302, {"Location": "/loop"}, b"")
    public.routes["/svg"] = (200, {"Content-Type": "image/svg+xml"}, b"<svg/>")
    public.routes["/html"] = (200, {"Content-Type": "text/html"}, b"<html>")
    public.routes["/big.png"] = (200, {"Content-Type": "image/png"}, b"x" * 2048)
    internal.routes["/secret.png"] = (200, {"Content-Type": "image/png"}, PNG)

    assert fetch(f"{public.url}/a.png", "test") == (PNG, "image/png")
    assert fetch(f"{public.url}/moved", "test") == (PNG, "image/png")  # 公開サーバー内のリダイレクトは追う
    # 内部のサーバーへのリダイレクトは拒否し、内部のサーバーには接続しない
    assert fetch(f"{public.url}/to-internal", "test") is None
    assert fetch(f"{internal.url}/secret.png", "test") is None
    assert internal.hits == []
    assert fetch(f"{public.url}/loop", "test") is None
    assert fetch(f"{public.url}/svg", "test") is None
    assert fetch(f"{public.url}/html", "test") is None
    assert fetch(f"{public.url}/missing.png", "test") is None
    assert fetch(f"{public.url}/big.png", "test", max_bytes=1024) is None


def test_fetch_failures_return_none(monkeypatch):
    monkeypatch.setattr(external_image, "blocked_address", lambda a: False)

    def boom(*args, **kwargs):
        raise requests.Timeout("slow")

    monkeypatch.setattr(external_image.requests, "get", boom)
    assert fetch("http://127.0.0.1:9/a.png", "test") is None


def test_local_server_endpoint_rejects_internal_hosts(db, monkeypatch):
    from remi.context import AppContext
    from remi.server.api import RemiApi
    from remi.server.http import LocalServer

    ctx = AppContext(db)
    server = LocalServer(RemiApi(ctx))
    server.start()
    try:
        s = requests.Session()
        s.trust_env = False
        s.get(server.start_url)
        target = f"{server.origin}/icon.svg"  # 自分自身(ループバック)
        ctx.settings.set("allow_external_images", True)
        assert s.get(f"{server.origin}/external-image", params={"url": target}).status_code == 404
        assert s.get(f"{server.origin}/external-image", params={"url": "file:///etc/passwd"}).status_code == 404
    finally:
        server.stop()
