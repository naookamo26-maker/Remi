"""エントリーポイント。`python -m remi` または `remi` で起動する。"""

from __future__ import annotations

import sys


def main() -> int:
    # requests より先に truststore を有効にする(設計書 §2.3)
    from remi.bootstrap import init_ssl

    init_ssl()

    from remi.app import run

    return run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
