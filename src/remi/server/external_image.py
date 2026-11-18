"""本文中の外部画像の取得(設定で許可した場合のみ。設計書 §5.3)。

チケットの本文は他の人が書けるので、本文中のURLで利用者のPCから意図しない接続先へアクセスさせられないよう、
接続先を検査する:

- スキームは http / https のみ
- 名前解決した後のIPアドレスで判定する(localhost や 10進表記のIPアドレスなど、表記の偽装も防ぐ)
- ループバック・リンクローカル(クラウドのメタデータサービスを含む)・未指定・マルチキャスト・予約済みは拒否する
- プライベートIPアドレス(社内のサーバー)は許可する。社内のRedmineでは社内サーバーの画像を貼ることが多いため
- リダイレクトは自動で追わず、移動先も同じ検査をしてから取得する

残る制約: 検査した後に、DNSの応答が別のIPアドレスに変わる(DNSリバインディング)場合は防げない。
プロキシを使う場合も、検査はこのPCでの名前解決の結果で行う。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urljoin, urlsplit

import requests

log = logging.getLogger(__name__)

MAX_BYTES = 10 * 1024 * 1024
TIMEOUT = (5, 20)  # (接続, 読み取り) 秒
MAX_REDIRECTS = 3
_THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")


def blocked_address(address: str) -> bool:
    """外部画像の取得先にしてはいけないIPアドレスか。"""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])  # IPv6のスコープ(fe80::1%eth0)は外して判定する
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 など
    return (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved
            or (isinstance(ip, ipaddress.IPv4Address) and ip in _THIS_NETWORK))


def url_allowed(url: str) -> bool:
    """URLの形式と、名前解決した接続先を検査する。1つでも拒否するアドレスがあれば拒否する。"""
    try:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return False
    addresses = {info[4][0] for info in infos}
    if not addresses or any(blocked_address(a) for a in addresses):
        log.info("external image blocked: %s", parts.hostname)
        return False
    return True


def fetch(url: str, user_agent: str, max_bytes: int = MAX_BYTES) -> tuple[bytes, str] | None:
    """画像を取得する。拒否・失敗したときは None(検査に通らなければ通信しない)。"""
    for _ in range(MAX_REDIRECTS + 1):
        if not url_allowed(url):
            return None
        try:
            with requests.get(url, timeout=TIMEOUT, stream=True, allow_redirects=False,
                              headers={"User-Agent": user_agent}) as r:
                if r.is_redirect:
                    location = r.headers.get("Location")
                    if not location:
                        return None
                    url = urljoin(url, location)
                    continue
                ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if r.status_code != 200 or not ctype.startswith("image/") or ctype == "image/svg+xml":
                    return None
                data = bytearray()
                for chunk in r.iter_content(64 * 1024):
                    data += chunk
                    if len(data) > max_bytes:
                        return None
                return bytes(data), ctype
        except requests.RequestException:
            return None
    return None  # リダイレクトが多すぎる
