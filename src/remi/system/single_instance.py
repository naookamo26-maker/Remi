"""多重起動の防止(設計書 §2.5)。

最初に起動したプロセスが名前付きパイプ(Windows)/ UNIXソケット(開発用)で待ち受ける。
2つ目のプロセスはそこへ依頼("show"、通知を押したときの ("open", サーバー, チケット)、
終了させる "quit")を送って終了する。
最初のプロセスは on_show を、依頼を引数にして呼ぶ。
"""

from __future__ import annotations

import getpass
import hashlib
import logging
import os
import sys
import tempfile
import threading
from collections.abc import Callable
from multiprocessing.connection import Client, Listener
from typing import Any

from remi import APP_NAME

log = logging.getLogger(__name__)

_AUTH = b"remi-single-instance"


def _valid(msg: Any) -> bool:
    """受け付ける依頼: "show"・"quit" か ("open", サーバーID, チケットID)。"""
    if msg in ("show", "quit"):
        return True
    return (isinstance(msg, tuple) and len(msg) == 3 and msg[0] == "open"
            and all(isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in msg[1:]))


def default_address() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "user"
    digest = hashlib.sha1(user.encode("utf-8")).hexdigest()[:12]
    if sys.platform == "win32":
        return rf"\\.\pipe\{APP_NAME}-{digest}"
    return os.path.join(tempfile.gettempdir(), f"{APP_NAME.lower()}-{digest}.sock")


class SingleInstance:
    def __init__(self, address: str | None = None) -> None:
        self.address = address or default_address()
        self._listener: Listener | None = None
        self._thread: threading.Thread | None = None

    def notify_existing(self, message: Any = "show") -> bool:
        """既に起動しているRemiがあれば依頼(既定は前面表示)を送って True を返す。"""
        try:
            conn = Client(self.address, authkey=_AUTH)
        except (OSError, EOFError):
            return False
        try:
            conn.send(message)
            conn.recv()  # 受け取ったことの確認を待つ
        except (OSError, EOFError):
            pass
        finally:
            conn.close()
        return True

    def listen(self, on_show: Callable[..., None]) -> bool:
        """待ち受ける。依頼が届くと on_show(依頼) を呼ぶ。"""
        if sys.platform != "win32" and os.path.exists(self.address):
            os.unlink(self.address)  # 前回の異常終了で残ったソケット
        try:
            self._listener = Listener(self.address, authkey=_AUTH)
        except OSError:
            log.exception("could not listen on %s", self.address)
            return False

        def serve() -> None:
            listener = self._listener
            while listener is not None:
                try:
                    conn = listener.accept()
                except (OSError, EOFError):
                    break
                except Exception:  # noqa: BLE001 - 認証失敗など
                    continue
                try:
                    msg = conn.recv()
                    if _valid(msg):
                        conn.send("ok")
                        on_show(msg)
                except (OSError, EOFError):
                    pass
                finally:
                    conn.close()

        self._thread = threading.Thread(target=serve, name="single-instance", daemon=True)
        self._thread.start()
        return True

    def acquire(self, on_show: Callable[..., None], message: Any = "show") -> bool:
        """このプロセスが起動を続けてよいか。既に起動しているRemiがあれば前面表示を依頼して False を返す。

        同時に起動したときは、両方が「既存なし」と判断しても、待ち受けられるのは1つだけ(Windowsの名前付きパイプ)。
        待ち受けに失敗したら、先に待ち受けた方へ依頼して終了する。
        """
        if self.notify_existing(message):
            return False
        if self.listen(on_show):
            return True
        if self.notify_existing(message):
            return False
        log.warning("could not listen for other instances; continuing without it")
        return True

    def close(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
