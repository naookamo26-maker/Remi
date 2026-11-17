"""ログの設定。ログは英語のみで出力する(設計書 §10.2)。"""

from __future__ import annotations

import logging
import logging.handlers

from remi import __version__
from remi.paths import log_dir


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if any(getattr(h, "_remi", False) for h in root.handlers):
        return
    handler = logging.handlers.RotatingFileHandler(
        log_dir() / "remi.log", maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler._remi = True  # type: ignore[attr-defined]
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level)
    # APIキーが含まれうるので、urllib3 のデバッグログは出さない
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger(__name__).info("Remi %s starting", __version__)
