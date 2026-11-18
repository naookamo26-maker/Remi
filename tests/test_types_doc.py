"""画面とバックエンドの間のデータの形(src/remi/web/js/types.js)が、実際の戻り値と一致するか。

types.js の @typedef の @property の名前を読み、APIの戻り値のキーと比べる。
[名前] の項目は省略できる(戻り値になくてもよい)。
"""

import re
import time
from pathlib import Path

import pytest

from fake_redmine import API_KEY, ME

TYPES_JS = Path(__file__).resolve().parents[1] / "src" / "remi" / "web" / "js" / "types.js"


def documented() -> dict[str, tuple[set[str], set[str]]]:
    """typedef名 → (必須の項目, 省略できる項目)。型の {...} は1段の入れ子まで読める。"""
    out = {}
    for block in TYPES_JS.read_text(encoding="utf-8").split("/**")[1:]:
        m = re.search(r"@typedef \{object\} (\w+)", block)
        if not m:
            continue
        required, optional = set(), set()
        for name in re.findall(r"@property \{(?:[^{}]|\{[^{}]*\})*\} (\[?\w+\]?)", block):
            (optional if name.startswith("[") else required).add(name.strip("[]"))
        out[m.group(1)] = (required, optional)
    return out


def check(name: str, value: dict) -> None:
    required, optional = documented()[name]
    keys = set(value)
    assert required <= keys, f"{name}: types.js にあるが戻り値にない項目 {sorted(required - keys)}"
    assert keys <= required | optional, f"{name}: 戻り値にあるが types.js にない項目 {sorted(keys - required - optional)}"


@pytest.fixture
def api(db, fake):
    from remi.context import AppContext
    from remi.server.api import RemiApi
    from remi.store.servers import Server
    from remi.sync.worker import SyncManager

    fake.add_project(1, "基幹")
    fake.add_project(2, "受注", parent=1)
    fake.add_issue(10, 2, "件名", description="説明", watchers={ME})
    fake.add_journal(10, 7, "コメント")
    fake.add_attachment(10, 77, "shot.png", b"png")
    fake.add_journal(10, 7, details=[{"property": "attachment", "name": "77", "new_value": "shot.png"}])
    ctx = AppContext(db)
    sync = SyncManager(db, ctx.bus)
    ctx.servers.save(Server(name="S", url=fake.base_url), api_key=API_KEY)
    sync.restart()
    end = time.monotonic() + 20
    while time.monotonic() < end and not ctx.issues.journals_synced(1, 10):
        time.sleep(0.05)
    yield RemiApi(ctx, sync)
    sync.stop_all()


def test_types_js_parses():
    names = documented()
    for name in ("TreeData", "TreeServer", "TreeProject", "IssueSummary", "ConversationData", "Journal",
                 "JournalDetail", "EditOptions", "WritePermissions", "FieldChange", "Upload", "TreeVersion",
                 "VersionData", "VersionEvent"):
        assert names.get(name, (set(), set()))[0] | names.get(name, (set(), set()))[1], name


def test_types_match_api_results(api):
    tree = api.getTree()
    check("TreeData", tree)
    check("TreeServer", tree["servers"][0])
    check("TreeProject", tree["servers"][0]["projects"][0])

    [issue] = api.listIssues()["issues"]
    check("IssueSummary", issue)

    conv = api.getConversation(serverId=1, issueId=10)
    check("ConversationData", conv)
    for j in conv["journals"]:
        check("Journal", j)
        for d in j["details"]:
            check("JournalDetail", d)
    assert any(j["details"] for j in conv["journals"])  # 添付の追加(JournalDetail)も確かめる

    opts = api.getEditOptions(serverId=1, issueId=10)
    check("EditOptions", opts)
    check("WritePermissions", opts["permissions"])


def test_version_types_match_api_results(api, fake):
    """バージョン(ロードマップ)の戻り値。"""
    from remi.sync.engine import ServerSync

    fake.add_version(5, 2, "v1.0")
    fake.add_issue(20, 2, "バージョンのチケット", assigned_to=7, version=5)
    api.ctx.projects.set_setting(1, 1, "watch_versions", True)
    engine = ServerSync(api.ctx.db, 1, api.sync.client(1))
    engine.sync_versions()
    fake.add_issue(21, 2, "追加", assigned_to=7)
    fake.set_version(21, 5)
    engine.sync_versions()
    [version] = api.getTree()["servers"][0]["projects"][0]["children"][0]["versions"]
    check("TreeVersion", version)
    data = api.getVersion(serverId=1, versionId=5)
    check("VersionData", data)
    check("VersionEvent", data["events"][0])
