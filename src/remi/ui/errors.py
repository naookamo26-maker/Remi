"""エラーコードを、利用者向けの(翻訳された)文言に変換する(設計書 §10.2)。"""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication

from remi.api.errors import ErrorCode, RedmineError


def error_message(err: RedmineError | ErrorCode) -> str:
    code = err.code if isinstance(err, RedmineError) else err
    messages = {
        ErrorCode.INVALID_URL: QCoreApplication.translate("Errors", "The URL is invalid. Enter an address that starts with http:// or https://."),
        ErrorCode.SSL_ERROR: QCoreApplication.translate("Errors", "The server certificate could not be verified. If your company uses its own "
                                         "certificate authority, check that it is installed in Windows."),
        ErrorCode.PROXY_ERROR: QCoreApplication.translate("Errors", "Could not connect through the proxy. Check the proxy settings."),
        ErrorCode.TIMEOUT: QCoreApplication.translate("Errors", "The server did not respond in time."),
        ErrorCode.CONNECTION_FAILED: QCoreApplication.translate("Errors", "Could not connect to the server. Check the URL and your network."),
        ErrorCode.UNAUTHORIZED: QCoreApplication.translate("Errors", "The API access key is incorrect."),
        ErrorCode.FORBIDDEN: QCoreApplication.translate("Errors", "You do not have permission for this operation."),
        ErrorCode.NOT_FOUND: QCoreApplication.translate("Errors", "The item was not found. It may have been deleted."),
        ErrorCode.REST_DISABLED: QCoreApplication.translate("Errors", "The REST API may be disabled on this Redmine. Ask the administrator "
                                             "to enable it in Administration > Settings > API."),
        ErrorCode.UNPROCESSABLE: QCoreApplication.translate("Errors", "The server rejected the input."),
        ErrorCode.SERVER_ERROR: QCoreApplication.translate("Errors", "An error occurred on the server."),
        ErrorCode.INVALID_RESPONSE: QCoreApplication.translate("Errors", "The server returned an unexpected response."),
        ErrorCode.TOO_LARGE: QCoreApplication.translate("Errors", "The file is too large to display."),
    }
    msg = messages.get(code, code.value)
    if isinstance(err, RedmineError) and err.code == ErrorCode.UNPROCESSABLE and err.detail:
        msg += f" ({err.detail})"
    return msg
