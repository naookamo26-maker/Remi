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
        self._connections: set[_Connection] = set()

    def notify_existing(self, timeout_ms: int = 500) -> bool:
        """既に起動しているRemiがあれば前面表示を依頼して True を返す。"""
        sock = QLocalSocket()
        sock.connectToServer(self.name)
        if not sock.waitForConnected(timeout_ms):
            return False
        sock.write(b"show\n")
        sock.flush()
        sock.waitForBytesWritten(timeout_ms)
        # 受け取ったことを相手が返すまで待つ(すぐ切断すると、Windowsでは届かないことがある)
        sock.waitForReadyRead(timeout_ms * 4)
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
            handler = _Connection(sock, self.activateRequested.emit)
            self._connections.add(handler)
            handler.finished = lambda h=handler: self._connections.discard(h)

    def close(self) -> None:
        for c in list(self._connections):
            c.dispose()
        self._connections.clear()
        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
            self._server = None


class _Connection:
    """受け付けた1本の接続。"show" を受け取ったら応答して閉じる。"""

    def __init__(self, sock: QLocalSocket, on_show) -> None:
        self.sock: QLocalSocket | None = sock
        self.on_show = on_show
        self.buf = b""
        self.finished = lambda: None
        sock.readyRead.connect(self._read)
        sock.disconnected.connect(self._read)
        # Windowsの名前付きパイプでは、受け付けた時点でデータが届いていて
        # readyRead が来ないことがあるので、ここでも読む
        if sock.bytesAvailable():
            self._read()

    def _read(self) -> None:
        if self.sock is None:
            return
        self.buf += bytes(self.sock.readAll().data())
        if b"show" in self.buf:
            self.sock.write(b"ok\n")
            self.sock.flush()
            self.on_show()
            self.dispose()
        elif self.sock.state() == QLocalSocket.LocalSocketState.UnconnectedState:
            self.dispose()

    def dispose(self) -> None:
        sock, self.sock = self.sock, None
        if sock is None:
            return
        sock.readyRead.disconnect(self._read)
        sock.disconnected.disconnect(self._read)
        sock.disconnectFromServer()
        sock.deleteLater()
        self.finished()
