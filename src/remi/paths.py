"""データの保存場所。

Windowsでは %LOCALAPPDATA%\\Remi、それ以外(開発・テスト)では ~/.local/share/Remi。
環境変数 REMI_DATA_DIR で上書きできる。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from remi import APP_NAME


def data_dir() -> Path:
    override = os.environ.get("REMI_DATA_DIR")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def db_path() -> Path:
    return data_dir() / "remi.db"


def log_dir() -> Path:
    p = data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def attachment_cache_dir(server_id: int | None = None) -> Path:
    p = data_dir() / "cache" / "attachments"
    if server_id is not None:
        p = p / str(server_id)
    p.mkdir(parents=True, exist_ok=True)
    return p
