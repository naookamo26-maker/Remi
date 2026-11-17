"""翻訳ファイルの管理(設計書 §10.3)。

    python tools/i18n.py update   # ソースから文字列を抽出して .ts を更新
    python tools/i18n.py build    # .ts から .qm を生成
    python tools/i18n.py check    # 未翻訳の文字列があれば失敗する(CI用)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "remi"
I18N = SRC / "i18n"
LANGUAGES = ["ja"]


def _tool(name: str) -> str:
    exe = Path(sys.executable).parent / (name + (".exe" if sys.platform == "win32" else ""))
    if exe.exists():
        return str(exe)
    found = shutil.which(name)
    if not found:
        sys.exit(f"{name} が見つかりません。PySide6 をインストールしてください。")
    return found


def sources() -> list[str]:
    return sorted(str(p.relative_to(ROOT)) for p in SRC.rglob("*.py"))


def update() -> None:
    for lang in LANGUAGES:
        ts = I18N / f"remi_{lang}.ts"
        cmd = [_tool("pyside6-lupdate"), *sources(), "-no-obsolete", "-locations", "none", "-ts", str(ts)]
        subprocess.run(cmd, check=True, cwd=ROOT)


def build() -> None:
    for lang in LANGUAGES:
        ts = I18N / f"remi_{lang}.ts"
        qm = I18N / f"remi_{lang}.qm"
        subprocess.run([_tool("pyside6-lrelease"), str(ts), "-qm", str(qm)], check=True, cwd=ROOT)


def check() -> int:
    problems = 0
    for lang in LANGUAGES:
        ts = I18N / f"remi_{lang}.ts"
        tree = ET.parse(ts)
        for ctx in tree.getroot().iter("context"):
            cname = ctx.findtext("name")
            for msg in ctx.iter("message"):
                tr = msg.find("translation")
                if tr is None or tr.get("type") in ("unfinished", "vanished", "obsolete") or not (
                    (tr.text or "").strip() or tr.findall("numerusform")
                ):
                    problems += 1
                    print(f"[{lang}] untranslated: {cname}: {msg.findtext('source')!r}")
    return 1 if problems else 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "update":
        update()
    elif cmd == "build":
        build()
    elif cmd == "check":
        sys.exit(check())
    else:
        sys.exit(__doc__)
