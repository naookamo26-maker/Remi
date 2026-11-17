"""起動直後に最初に実行する初期化処理。

requests / urllib3 を import する前に呼ぶこと(設計書 §2.3)。
PyInstaller では runtime hook からも呼ばれるため、何度呼ばれても安全にしてある。
"""

from __future__ import annotations

_ssl_initialized = False


def init_ssl() -> bool:
    """Windowsの証明書ストアをPythonのSSLで使えるようにする。成功したら True。"""
    global _ssl_initialized
    if _ssl_initialized:
        return True
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:  # noqa: BLE001 - 失敗してもアプリは起動させ、接続テストで原因を表示する
        return False
    _ssl_initialized = True
    return True
