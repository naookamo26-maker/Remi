"""APIクライアントのエラー。

文字列ではなくエラーコードを返し、表示用の文言はUI層で翻訳する(設計書 §10.2)。
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(Enum):
    INVALID_URL = "invalid_url"
    SSL_ERROR = "ssl_error"
    PROXY_ERROR = "proxy_error"
    TIMEOUT = "timeout"
    CONNECTION_FAILED = "connection_failed"
    UNAUTHORIZED = "unauthorized"  # 401: APIキーが誤っている
    FORBIDDEN = "forbidden"  # 403: 権限がない
    NOT_FOUND = "not_found"  # 404
    REST_DISABLED = "rest_disabled"  # 接続テストで 403/404 が返った: REST APIが無効の可能性
    UNPROCESSABLE = "unprocessable"  # 422: 入力内容のエラー
    SERVER_ERROR = "server_error"  # 5xx
    INVALID_RESPONSE = "invalid_response"  # JSONでない応答など
    TOO_LARGE = "too_large"  # 添付ファイルが上限を超えた
    OTHER_HOST = "other_host"  # 登録したRedmine以外へAPIキーを送ろうとした(リダイレクトなど)
    CANCELLED = "cancelled"  # 同期を止めたので、送らなかった・結果を捨てた


class RedmineError(Exception):
    def __init__(self, code: ErrorCode, status: int | None = None, detail: str = "") -> None:
        super().__init__(f"{code.value} (status={status}) {detail}".strip())
        self.code = code
        self.status = status
        self.detail = detail

    @property
    def retryable(self) -> bool:
        return self.code in (
            ErrorCode.TIMEOUT,
            ErrorCode.CONNECTION_FAILED,
            ErrorCode.PROXY_ERROR,
            ErrorCode.SERVER_ERROR,
        )
