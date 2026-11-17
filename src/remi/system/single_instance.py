"""多重起動の防止(設計書 §2.5)。

最初に起動したプロセスが QLocalServer を立てる。2つ目のプロセスはそこへ "show" を送り、
すぐに終了する。最初のプロセスは activateRequested を受けてウィンドウを前面に出す。
"""

from __future__ import annotations

import getpass
import hashlib

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from remi import APP_NAME


def default_server_name() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "user"
    digest = hashlib.sha1(user.encode("utf-8")).hexdigest()[:12]
    return f"{APP_NAME}-{digest}"


class SingleInstance(QObject):
    activateRequested = Signal()

    def __init__(self, name: str | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.name = name or default_server_name()
        self._server: QLocalServer | None = None

    def notify_existing(self, timeout_ms: int = 500) -> bool:
        """既に起動しているRemiがあれば前面表示を依頼して True を返す。"""
        sock = QLocalSocket()
        sock.connectToServer(self.name)
        if not sock.waitForConnected(timeout_ms):
            return False
        sock.write(b"show\n")
        sock.flush()
        sock.waitForBytesWritten(timeout_ms)
        sock.disconnectFromServer()
        return True

    def listen(self) -> bool:
        server = QLocalServer(self)
        if not server.listen(self.name):
            # 前回の異常終了でソケットが残っている場合
            QLocalServer.removeServer(self.name)
            if not server.listen(self.name):
                return False
        server.newConnection.connect(self._on_connection)
        self._server = server
        return True

    def _on_connection(self) -> None:
        assert self._server is not None
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            sock._remi_buf = b""  # type: ignore[attr-defined]
            sock.readyRead.connect(lambda s=sock: self._on_ready(s))
            sock.disconnected.connect(lambda s=sock: self._on_ready(s))
            # Windowsの名前付きパイプでは、受け付けた時点でデータが届いていることがあり、
            # その場合 readyRead が来ないので、ここでも読む
            if sock.bytesAvailable():
                self._on_ready(sock)

    def _on_ready(self, sock: QLocalSocket) -> None:
        buf = sock._remi_buf + bytes(sock.readAll().data())  # type: ignore[attr-defined]
        if b"show" in buf and not getattr(sock, "_remi_done", False):
            sock._remi_done = True  # type: ignore[attr-defined]
            self.activateRequested.emit()
            sock.disconnectFromServer()
            sock.deleteLater()
            return
        sock._remi_buf = buf  # type: ignore[attr-defined]

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
