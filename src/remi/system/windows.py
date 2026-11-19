"""Windows固有の処理。Windows以外では何もしない(開発・テスト用)。"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
PERSONALIZE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
APP_VALUE = "Remi"
DWMWA_USE_IMMERSIVE_DARK_MODE = 20


def _read_dword(value: str) -> int | None:
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSONALIZE_KEY) as key:
            return int(winreg.QueryValueEx(key, value)[0])
    except OSError:
        return None


def apps_use_dark() -> bool:
    """Windowsのアプリの色がダークか(AppsUseLightTheme = 0)。"""
    return _read_dword("AppsUseLightTheme") == 0


def taskbar_uses_dark() -> bool:
    """タスクバーの色がダークか(SystemUsesLightTheme = 0)。トレイアイコンの色に使う(設計書 §9.3)。"""
    v = _read_dword("SystemUsesLightTheme")
    return v is None or v == 0


def set_dark_title_bar(hwnd: int, dark: bool) -> None:
    if sys.platform != "win32" or not hwnd:
        return
    try:
        import ctypes

        value = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:  # noqa: BLE001
        log.debug("DwmSetWindowAttribute failed", exc_info=True)


def launch_command() -> str:
    """Remiを起動するコマンド。exe化されていればexe、そうでなければ pythonw -m remi。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{exe}" -m remi'


def autostart_command() -> str:
    """自動起動で登録するコマンド。"""
    return f"{launch_command()} --background"


CLASSES_KEY = r"Software\Classes"


def _set_values(path: str, values: dict[str, str]) -> None:
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
        for name, value in values.items():
            winreg.SetValueEx(key, name or None, 0, winreg.REG_SZ, value)


def register_notifications(aumid: str, display_name: str, icon_path: str | None, protocol: str) -> None:
    """通知の登録(HKCU なので管理者権限は要らない。起動のたびに最新の場所で登録し直す)。

    - AppUserModelID: 登録したIDで通知を出すと、通知センターでアプリ名とアイコンが出て、履歴にも正しく残る
    - プロトコル(remi:): 通知を押すと `<exe> --open "remi://open?..."` を起動する。トーストが消えた後の履歴から
      押したとき・アプリを終了した後でも届く(起動中なら、2つ目のプロセスが起動中のRemiへ依頼して終わる)
    """
    if sys.platform != "win32":
        return
    try:
        values = {"DisplayName": display_name}
        if icon_path:
            values["IconUri"] = icon_path
        _set_values(rf"{CLASSES_KEY}\AppUserModelId\{aumid}", values)
        base = rf"{CLASSES_KEY}\{protocol}"
        _set_values(base, {"": f"URL:{display_name}", "URL Protocol": ""})
        if icon_path:
            _set_values(rf"{base}\DefaultIcon", {"": icon_path})
        _set_values(rf"{base}\shell\open\command", {"": f'{launch_command()} --open "%1"'})
    except OSError:
        log.exception("could not register notifications")


def set_autostart(enabled: bool) -> None:
    if sys.platform != "win32":
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_VALUE, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, APP_VALUE)
            except FileNotFoundError:
                pass


def message_box(title: str, text: str) -> None:
    """GUIが立ち上がる前のエラー表示。"""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    else:
        print(f"{title}: {text}", file=sys.stderr)
