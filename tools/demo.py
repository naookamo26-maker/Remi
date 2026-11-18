"""見た目の確認用: サンプルデータ入りの偽Redmineにつないだ画面を起動する。

    python tools/demo.py            # URLを表示して待つ(ブラウザで開く)
    python tools/demo.py --port 8765

データは一時フォルダに作り、終了すると消える。テストの偽Redmine(tests/fake_redmine.py)を使う。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))


def ts(minutes_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def seed(fake) -> None:
    from fake_redmine import ME

    fake.add_project(1, "基幹システム")
    fake.add_project(2, "受注管理", parent=1)
    fake.add_project(3, "在庫管理", parent=1)
    fake.add_project(4, "社内ポータル")
    for pid in (1, 2, 3, 4):
        fake.members[pid] = [ME, 7, 8, 100]
    # カテゴリとカスタムフィールド(「詳細」で表示・編集する)
    fake.categories[2] = [{"id": 1, "name": "画面"}, {"id": 2, "name": "帳票"}, {"id": 3, "name": "バッチ"}]
    fake.custom_fields = [
        {"id": 1, "name": "顧客", "field_format": "string"},
        {"id": 2, "name": "発生環境", "field_format": "list", "multiple": True,
         "possible_values": [{"value": "本番"}, {"value": "検証"}, {"value": "開発"}]},
        {"id": 3, "name": "要リリース", "field_format": "bool"},
    ]
    fake.add_issue(1234, 2, "受注一覧のCSV出力で文字化けする", watchers={ME}, updated_on=ts(3000),
                   description="受注一覧からCSVを出力すると、Excelで開いたときに文字化けします。",
                   custom_fields=[{"id": 1, "name": "顧客", "value": "山田商事"},
                                  {"id": 2, "name": "発生環境", "multiple": True, "value": ["本番"]},
                                  {"id": 3, "name": "要リリース", "value": "1"}])
    fake.set_status(1234, 2)
    fake.add_journal(1234, 7, "再現手順です。\n\n1. 受注一覧で `得意先名` に「髙橋」を含む行を表示\n2. 「CSV出力」を押す\n"
                     "3. Excelで開くと **文字化け** する\n\n出力処理はこのあたりです。\n\n```python\n# export.py\n"
                     "def write_csv(rows, path):\n    with open(path, \"w\", encoding=\"utf-8\") as f:\n"
                     "        csv.writer(f).writerows(rows)\n```", created_on=ts(2990))
    png = (ROOT / "tools" / "demo_screenshot.png")
    if png.exists():
        fake.add_attachment(1234, 501, "excel_mojibake.png", png.read_bytes())
        fake.add_journal(1234, 7, "Excelで開いたときの画面です。\n\n![](excel_mojibake.png)", created_on=ts(2985),
                         details=[{"property": "attachment", "name": "501", "new_value": "excel_mojibake.png"}])
    fake.add_journal(1234, 8, "顧客からは **今週中の修正** を求められています。#1198 の対応と合わせてリリースできますか?",
                     private=True, created_on=ts(30))
    fake.add_journal(1234, ME, "@tanaka 確認しました。#1198 と同じく BOM なしの UTF-8 が原因です。"
                     "`encoding=\"utf-8-sig\"` に変更します。", created_on=ts(25))
    # 1回の更新で複数の項目を変えた(まとめて1つの変更履歴として表示される)
    fake.add_journal(1234, 7, created_on=ts(20), details=[
        {"property": "attr", "name": "status_id", "old_value": "1", "new_value": "2"},
        {"property": "attr", "name": "priority_id", "old_value": "2", "new_value": "3"},
        {"property": "attr", "name": "assigned_to_id", "old_value": "7", "new_value": str(ME)},
    ])
    fake.add_journal(1234, 7, "ありがとうございます!テスト環境で確認します。", created_on=ts(2))
    fake.add_issue(1229, 2, "受注確定メールの宛先に営業担当を追加したい", assigned_to=7, watchers={ME}, updated_on=ts(45))
    fake.add_journal(1229, 8, "仕様を確認しました。来週着手します。", created_on=ts(44))
    fake.add_issue(1198, 2, "帳票出力の文字コードをShift_JISに統一する", assigned_to=100, updated_on=ts(1500))
    fake.add_issue(1175, 3, "受注検索の日付範囲に「今月」プリセットを追加", assigned_to=7, author=ME, watchers={ME},
                   updated_on=ts(4000))
    fake.add_issue(1301, 3, "在庫引当のバッチが二重起動する", updated_on=ts(10))
    fake.add_journal(1301, 8, "ログを添付します。attachment:batch.log を確認してください。", created_on=ts(9))
    fake.add_attachment(1301, 502, "batch.log", b"2026-11-17 10:00:00 start\n", "text/plain")
    for i in range(12):
        fake.add_issue(1400 + i, 4, f"ポータルの改善要望 {i + 1}", assigned_to=None, watchers={ME}, updated_on=ts(6000 + i * 60))
    # ロードマップ(バージョン)。自分と関係ないチケットも含む
    fake.add_version(31, 2, "v1.1", status="closed", due_date="2026-10-31")
    fake.add_version(32, 2, "v1.2", due_date="2026-12-15")
    fake.add_version(33, 3, "在庫 2026年冬")
    fake.add_issue(1500, 2, "v1.1 リリース作業", assigned_to=8, status_id=5, version=31, updated_on=ts(30000))
    for iid in (1234, 1229):
        fake.set_version(iid, 32)
    fake.add_issue(1501, 2, "受注画面のレスポンス改善", assigned_to=8, version=32, updated_on=ts(500))
    fake.add_issue(1502, 2, "得意先マスタの一括更新", assigned_to=7, status_id=5, version=32, updated_on=ts(900))
    fake.add_issue(1503, 2, "受注番号の採番ルール変更", assigned_to=7, version=32, updated_on=ts(700))
    fake.add_issue(1504, 3, "棚卸しCSVの取り込み", assigned_to=8, version=33, updated_on=ts(800))


def change_versions(fake) -> None:
    """バージョンの変化の見本: 追加・外れた・完了・期日の変更。"""
    fake.add_issue(1505, 2, "受注一覧に納期の列を追加", assigned_to=8, updated_on=ts(1))
    fake.set_version(1505, 32)
    fake.set_version(1503, None)
    fake.set_status(1501, 5)
    fake.update_version(32, due_date="2026-12-22")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--lang", default="ja")
    p.add_argument("--theme", default="system")
    p.add_argument("--unread", action="store_true", help="2件のコメントを未読にしておく")
    p.add_argument("--versions", action="store_true", help="基幹システムのバージョンをウォッチし、変化を記録しておく")
    p.add_argument("--admin", action="store_true", help="管理者として接続する(カスタムフィールドの形式が分かる)")
    args = p.parse_args()

    tmp = tempfile.mkdtemp(prefix="remi-demo-")
    os.environ["REMI_DATA_DIR"] = tmp
    import keyring

    store: dict = {}
    keyring.get_password = lambda s, u: store.get((s, u))
    keyring.set_password = lambda s, u, pw: store.__setitem__((s, u), pw)

    from fake_redmine import API_KEY, FakeRedmine

    from remi.api.client import RedmineClient
    from remi.context import AppContext
    from remi.server.api import RemiApi
    from remi.server.http import LocalServer
    from remi.store.db import open_database
    from remi.store.servers import Server
    from remi.sync.engine import ServerSync
    from remi.sync.worker import SyncManager

    fake = FakeRedmine()
    fake.start()
    seed(fake)
    fake.admin = args.admin
    db = open_database(Path(tmp) / "remi.db")
    ctx = AppContext(db)
    ctx.settings.set("language", args.lang)
    ctx.settings.set("theme", args.theme)
    ctx.servers.save(Server(name="社内Redmine", url=fake.base_url, text_format="common_mark"), api_key=API_KEY)
    sync = SyncManager(db, ctx.bus)
    server = LocalServer(RemiApi(ctx, sync), port=args.port)
    server.start()
    sync.restart()
    # 同期が終わるのを待つ
    end = time.monotonic() + 30
    while time.monotonic() < end and not all(ctx.issues.journals_synced(1, i) for i in (1234, 1229, 1301)):
        time.sleep(0.2)
    if args.unread:
        fake.add_journal(1229, 7, "追加の確認事項があります。宛先は CC にしますか?")
        fake.add_journal(1301, 7, "@okamo 本番でも再現しました。至急お願いします。")
        sync.sync_now()
        end = time.monotonic() + 30
        while time.monotonic() < end and not (ctx.issues.unread_count(1, 1229) and ctx.issues.unread_count(1, 1301)):
            time.sleep(0.2)
    if args.versions:
        ctx.projects.set_setting(1, 1, "watch_versions", True)
        engine = ServerSync(db, 1, RedmineClient(fake.base_url, API_KEY, min_interval=0))
        engine.sync_versions()
        change_versions(fake)
        engine.sync_versions()
        engine.client.close()
        ctx.bus.publish("versionsChanged", serverId=1, versionIds=[])
    print(server.start_url, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        sync.stop_all()
        server.stop()
        fake.stop()


if __name__ == "__main__":
    main()
