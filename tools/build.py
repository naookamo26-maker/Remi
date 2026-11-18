"""Windows向けの配布物を作る(設計書 §2.5、開発ステップ9)。

    python tools/build.py

- PyInstaller の --onedir で dist/Remi/ を作り、dist/Remi-<バージョン>-win64.zip にまとめる
- exe のファイルバージョンは remi.__version__ から作る(Windowsの4桁形式 X.Y.Z.0)
- アイコン(.ico)はトレイと同じ描画処理から作る
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from remi import APP_NAME, __version__  # noqa: E402

BUILD = ROOT / "build"
DIST = ROOT / "dist"


def version_tuple() -> tuple[int, int, int, int]:
    major, minor, patch = (int(x) for x in __version__.split("."))
    return major, minor, patch, 0


def write_version_file(path: Path) -> None:
    v = version_tuple()
    s = ".".join(map(str, v))
    path.write_text(f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041104B0', [
      StringStruct('CompanyName', ''),
      StringStruct('FileDescription', '{APP_NAME} - Redmine chat-style client'),
      StringStruct('FileVersion', '{s}'),
      StringStruct('InternalName', '{APP_NAME}'),
      StringStruct('OriginalFilename', '{APP_NAME}.exe'),
      StringStruct('ProductName', '{APP_NAME}'),
      StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [0x0411, 1200])])
  ]
)
""", encoding="utf-8")


def write_icon(path: Path) -> None:
    from remi.system.tray import make_icon

    img = make_icon(unread=False, dark_taskbar=False, size=256)
    img.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def main() -> None:
    BUILD.mkdir(exist_ok=True)
    version_file = BUILD / "version_info.txt"
    icon = BUILD / "remi.ico"
    write_version_file(version_file)
    write_icon(icon)
    src = ROOT / "src" / "remi"
    sep = ";" if sys.platform == "win32" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
        "--name", APP_NAME,
        "--icon", str(icon),
        "--version-file", str(version_file),
        "--runtime-hook", str(ROOT / "packaging" / "hook_truststore.py"),
        "--add-data", f"{src / 'web'}{sep}remi/web",
        "--add-data", f"{src / 'i18n' / '*.json'}{sep}remi/i18n",
        "--collect-submodules", "remi",
        "--collect-data", "pygments",
        "--hidden-import", "linkify_it",
        "--paths", str(ROOT / "src"),
        "--distpath", str(DIST), "--workpath", str(BUILD / "pyinstaller"), "--specpath", str(BUILD),
        str(ROOT / "packaging" / "entry.py"),
    ]
    subprocess.run(cmd, check=True, cwd=ROOT)
    out = DIST / APP_NAME
    archive = DIST / f"{APP_NAME}-{__version__}-win64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for f in out.rglob("*"):
            z.write(f, Path(APP_NAME) / f.relative_to(out))
    print(archive)


if __name__ == "__main__":
    main()
