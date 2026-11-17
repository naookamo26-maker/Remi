"""エントリーポイント。`python -m remi` または `remi` で起動する。"""

from __future__ import annotations

import sys


def main() -> int:
    # ウィンドウ版のexe(--windowed)では標準出力がないので、書き込みで落ちないようにする
    import os

    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115
    # requests より先に truststore を有効にする(設計書 §2.3)
    from remi.bootstrap import init_ssl

    init_ssl()

    from remi.app import run

    return run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
