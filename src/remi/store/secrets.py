"""APIキーの保存(Windows資格情報マネージャー。設計書 §2.2)。DBには置かない。"""

from __future__ import annotations

import keyring
import keyring.errors

from remi import APP_NAME

SERVICE = APP_NAME


def _user(server_id: int) -> str:
    return f"server:{server_id}"


def get_api_key(server_id: int) -> str | None:
    return keyring.get_password(SERVICE, _user(server_id))


def set_api_key(server_id: int, api_key: str) -> None:
    keyring.set_password(SERVICE, _user(server_id), api_key)


def delete_api_key(server_id: int) -> None:
    try:
        keyring.delete_password(SERVICE, _user(server_id))
    except keyring.errors.PasswordDeleteError:
        pass
