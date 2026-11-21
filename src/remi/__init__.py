"""Remi — Redmineのチケットをチャット感覚で監視・コメントするデスクトップアプリ。"""

# バージョンの定義はここだけ(セマンティックバージョニング。設計書 §11)。
# ひとまずの完成までメジャーバージョンは 0 のままにする。
__version__ = "0.23.0"

APP_NAME = "Remi"
# Windowsのトースト通知やタスクバーのグループ化に使うID
APP_USER_MODEL_ID = "Remi.Remi"


def user_agent() -> str:
    return f"{APP_NAME}/{__version__}"
