"""画面のE2Eテスト(設計書 §2.6)。

ローカルAPIサーバーを偽のRedmineにつないで起動し、Playwright(Chromium)で画面を操作する。
Chromium は環境変数 REMI_CHROMIUM、既知の場所、Playwright の既定の順に探す。
"""

from __future__ import annotations

import glob
import os
import time

import pytest

from fake_redmine import API_KEY, ME

playwright_api = pytest.importorskip("playwright.sync_api")


def _chromium_path() -> str | None:
    if os.environ.get("REMI_CHROMIUM"):
        return os.environ["REMI_CHROMIUM"]
    found = glob.glob("/opt/pw-browsers/chromium-*/chrome-linux64/chrome")
    return found[0] if found else None


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(executable_path=_chromium_path())
        except Exception as e:  # noqa: BLE001
            if os.environ.get("CI"):
                raise
            pytest.skip(f"Chromium is not available: {e}")
        yield b
        b.close()


@pytest.fixture
def remi(db, fake):
    from remi.context import AppContext
    from remi.server.api import RemiApi
    from remi.server.http import LocalServer
    from remi.sync.worker import SyncManager

    ctx = AppContext(db)
    ctx.settings.set("language", "ja")
    sync = SyncManager(db, ctx.bus)
    server = LocalServer(RemiApi(ctx, sync))
    server.start()
    yield type("Remi", (), {"ctx": ctx, "sync": sync, "server": server})()
    sync.stop_all()
    server.stop()


@pytest.fixture
def page(browser, remi):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="ja-JP")
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.errors = errors
    yield pg
    ctx.close()


def seed(fake):
    fake.add_project(1, "基幹システム")
    fake.add_project(2, "受注管理", parent=1)
    fake.add_issue(10, 2, "CSVが文字化けする", description="説明です", watchers={ME})
    fake.add_journal(10, 7, "再現手順です\n\n```python\nprint('x')\n```\n\n![画面](shot.png)")
    fake.add_attachment(10, 77, "shot.png", _png())
    fake.add_issue(11, 2, "メールの宛先を追加したい", assigned_to=7, watchers={ME})


def _png() -> bytes:
    import base64

    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def add_server(remi, fake):
    from remi.store.servers import Server

    remi.ctx.servers.save(Server(name="社内Redmine", url=fake.base_url), api_key=API_KEY)
    remi.sync.restart()
    end = time.monotonic() + 20
    while time.monotonic() < end and not all(remi.ctx.issues.journals_synced(1, i) for i in (10, 11)):
        time.sleep(0.1)


def test_register_server_from_welcome(page, remi, fake):
    seed(fake)
    page.goto(remi.server.start_url)
    page.get_by_role("button", name="Redmineサーバーを登録").click()
    dialog = page.get_by_role("dialog", name="サーバーの登録")
    dialog.get_by_label("URL", exact=True).fill(fake.base_url)
    dialog.get_by_label("APIアクセスキー").fill("wrong")
    dialog.get_by_role("button", name="接続テスト").click()
    playwright_api.expect(dialog.get_by_role("status")).to_contain_text("APIアクセスキーが正しくありません")
    playwright_api.expect(dialog.get_by_role("button", name="登録")).to_be_disabled()

    dialog.get_by_label("APIアクセスキー").fill(API_KEY)
    dialog.get_by_role("button", name="接続テスト").click()
    playwright_api.expect(dialog.get_by_role("status")).to_contain_text("接続できました")
    dialog.get_by_label("表示名").fill("社内Redmine")
    dialog.get_by_role("button", name="登録").click()
    playwright_api.expect(dialog).to_be_hidden()

    playwright_api.expect(page.locator(".tree")).to_contain_text("社内Redmine")
    playwright_api.expect(page.locator(".issue")).to_have_count(2, timeout=20000)
    playwright_api.expect(page.locator(".tree")).to_contain_text("受注管理")
    assert not page.errors, page.errors


def test_conversation_post_and_live_update(page, remi, fake):
    seed(fake)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.locator(".issue", has_text="CSVが文字化けする").click()
    conv = page.locator(".pane-conv")
    playwright_api.expect(conv.locator("h1")).to_have_text("CSVが文字化けする")
    playwright_api.expect(conv.locator("pre.code")).to_contain_text("print")
    img = conv.locator(".bubble img")
    playwright_api.expect(img).to_have_attribute("src", "/attachments/1/77")
    page.wait_for_function  # noqa: B018 - CSPでevalできないので、下で直接確認する
    for _ in range(100):
        if img.evaluate("i => i.complete && i.naturalWidth > 0"):
            break
        time.sleep(0.1)
    assert img.evaluate("i => i.naturalWidth") == 1

    # 投稿(Ctrl+Enter)
    box = conv.get_by_role("textbox", name="コメント")
    box.fill("**対応します**")
    conv.get_by_role("tab", name="プレビュー").click()
    playwright_api.expect(conv.locator(".preview strong")).to_have_text("対応します")
    conv.get_by_role("tab", name="編集").click()
    box.press("Control+Enter")
    playwright_api.expect(conv.locator(".msg.mine .bubble", has_text="対応します")).to_be_visible(timeout=20000)
    playwright_api.expect(box).to_have_value("")
    assert fake.journals[10][-1]["notes"] == "**対応します**"

    # 他の人のコメントが届くと、開いている会話に追加される
    fake.add_journal(10, 8, "了解です")
    remi.sync.sync_now()
    playwright_api.expect(conv.locator(".bubble", has_text="了解です")).to_be_visible(timeout=30000)

    # 別のチケットに届いたコメントは、一覧とツリーの未読バッジになる
    fake.add_journal(11, 8, "こちらも見てください")
    remi.sync.sync_now()
    row = page.locator(".issue", has_text="メールの宛先")
    playwright_api.expect(row.locator(".badge")).to_have_text("1", timeout=30000)
    playwright_api.expect(page.locator(".node", has_text="未読のみ").locator(".badge")).to_have_text("1")
    row.click()
    playwright_api.expect(row.locator(".badge")).to_have_count(0, timeout=10000)
    assert not page.errors, page.errors


def test_failed_post_keeps_draft_and_retry(page, remi, fake):
    seed(fake)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.locator(".issue", has_text="CSVが文字化けする").click()
    conv = page.locator(".pane-conv")
    # 編集の選択肢の読み込み(Redmineへの問い合わせ)が終わってから、次の送信を失敗させる
    playwright_api.expect(conv.get_by_label("ステータス", exact=True)).to_be_enabled(timeout=15000)
    fake.fail_next_put = 500
    conv.get_by_role("textbox", name="コメント").fill("失敗するコメント")
    conv.get_by_role("button", name="送信").click()
    playwright_api.expect(conv.get_by_role("alert")).to_contain_text("送信できませんでした", timeout=20000)
    playwright_api.expect(conv.get_by_role("textbox", name="コメント")).to_have_value("失敗するコメント")
    conv.get_by_role("button", name="再送").click()
    playwright_api.expect(conv.locator(".msg.mine .bubble", has_text="失敗するコメント")).to_be_visible(timeout=20000)


def test_sanitized_html_does_not_run_scripts(page, remi, fake):
    seed(fake)
    fake.add_journal(10, 7, '<img src=x onerror="window.__xss=1"> <script>window.__xss=2</script> [x](javascript:window.__xss=3)')
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.locator(".issue", has_text="CSVが文字化けする").click()
    page.locator(".pane-conv .bubble").last.wait_for()
    for a in page.locator(".pane-conv .bubble a").all():
        a.click()
    time.sleep(0.3)
    assert page.evaluate("() => window.__xss") is None


def test_theme_and_language_switch(page, remi, fake):
    seed(fake)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.get_by_role("button", name="設定").first.click()
    dialog = page.get_by_role("dialog", name="設定")
    dialog.get_by_label("外観").select_option("dark")
    playwright_api.expect(page.locator("html")).to_have_class("theme-dark")
    dialog.get_by_label("言語").select_option("en")
    page.wait_for_load_state()
    playwright_api.expect(page.get_by_role("button", name="Settings")).to_be_visible(timeout=10000)
    playwright_api.expect(page.locator(".tree")).to_contain_text("Unread only")


def test_project_menu_sets_watch_authored(page, remi, fake):
    seed(fake)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.locator(".node", has_text="基幹システム").click(button="right")
    menu = page.get_by_role("menu")
    group = menu.get_by_role("group", name="作成したチケットを監視")
    group.get_by_role("button", name="オン").click()
    for _ in range(50):
        if remi.ctx.projects.get_setting(1, 1, "watch_authored"):
            break
        time.sleep(0.1)
    assert remi.ctx.projects.get_setting(1, 1, "watch_authored") is True
    assert remi.ctx.projects.effective_setting(1, 2, "watch_authored") is True


PASTE_JS = """(b64) => {
  const bin = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const dt = new DataTransfer();
  dt.items.add(new File([bin], "image.png", {type: "image/png"}));
  document.querySelector('.composer textarea').dispatchEvent(
    new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
}"""


def test_relation_filter(page, remi, fake):
    seed(fake)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    playwright_api.expect(page.locator(".issue")).to_have_count(2)
    filters = page.get_by_role("group", name="関係で絞り込み")
    filters.get_by_role("button", name="担当").click()
    playwright_api.expect(page.locator(".issue")).to_have_count(1)
    playwright_api.expect(page.locator(".issue")).to_contain_text("CSVが文字化けする")
    filters.get_by_role("button", name="ウォッチ").click()
    playwright_api.expect(page.locator(".issue")).to_have_count(2)
    filters.get_by_role("button", name="作成").click()
    playwright_api.expect(page.locator(".issue")).to_have_count(0)
    assert remi.ctx.settings.get("relation_filter") == "authored"


def test_change_status_paste_image_and_watch(page, remi, fake):
    import base64

    seed(fake)
    fake.members[2] = [ME, 7]
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.locator(".issue", has_text="メールの宛先").click()
    conv = page.locator(".pane-conv")
    status = conv.get_by_label("ステータス", exact=True)
    playwright_api.expect(status).to_be_enabled(timeout=15000)
    status.select_option(label="進行中")
    conv.get_by_label("担当者", exact=True).select_option(label="自分に割り当て")
    playwright_api.expect(conv.locator(".chip")).to_have_count(2)

    page.evaluate(PASTE_JS, base64.b64encode(_png()).decode())
    chip_img = conv.locator(".chip img")
    playwright_api.expect(chip_img).to_be_visible(timeout=15000)
    box = conv.get_by_role("textbox", name="コメント")
    playwright_api.expect(box).to_have_value(__import__("re").compile(r"!\[\]\(clipboard-\d{12}-\w{5}\.png\)"))
    conv.get_by_role("tab", name="プレビュー").click()
    playwright_api.expect(conv.locator(".preview img")).to_have_count(1)
    conv.get_by_role("tab", name="編集").click()

    conv.get_by_role("button", name="送信").click()
    playwright_api.expect(conv.locator(".chip")).to_have_count(0, timeout=20000)
    issue = fake.issues[11]
    assert issue["status"]["id"] == 2 and issue["assigned_to"]["id"] == ME
    last = fake.journals[11][-1]
    assert any(d["property"] == "attachment" for d in last["details"])
    playwright_api.expect(conv.locator(".bubble img").last).to_be_visible(timeout=20000)
    playwright_api.expect(conv.locator(".system", has_text="ステータス")).to_have_count(1)

    # ウォッチの切り替え
    watch = conv.get_by_role("button", name="ウォッチ中")
    watch.click()
    playwright_api.expect(conv.get_by_role("button", name="ウォッチする")).to_be_visible(timeout=15000)
    assert ME not in fake.watchers[11]
    assert not page.errors, page.errors


def test_open_redmine_menu(page, remi, fake, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    seed(fake)
    add_server(remi, fake)
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.goto(remi.server.start_url)
    page.locator(".issue", has_text="CSVが文字化けする").click()
    conv = page.locator(".pane-conv")
    conv.get_by_role("button", name="Redmineで開く").click()
    for _ in range(50):
        if opened:
            break
        time.sleep(0.1)
    # ウィンドウを持たない環境では既定のブラウザで開く
    assert opened == [f"{fake.base_url}/issues/10"]
    conv.get_by_role("button", name="その他の操作").click()
    page.get_by_role("menuitem", name="URLをコピー").click()
    playwright_api.expect(page.get_by_role("status")).to_contain_text("コピーしました")
    assert page.evaluate("() => navigator.clipboard.readText()") == f"{fake.base_url}/issues/10"


def test_live_update_keeps_composer_state(page, remi, fake):
    """同じチケットに更新が届いても、入力欄は作り直さない(入力中のフォーカス・カーソル・プレビューを保つ)。"""
    seed(fake)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.locator(".issue", has_text="CSVが文字化けする").click()
    conv = page.locator(".pane-conv")
    box = conv.get_by_role("textbox", name="コメント")
    box.click()
    box.press_sequentially("入力中")
    box.press("ArrowLeft")
    assert page.evaluate("getComputedStyle(document.querySelector('.chips')).display") == "none"
    fake.add_journal(10, 8, "届いたコメント")
    remi.sync.sync_now()
    playwright_api.expect(conv.locator(".bubble", has_text="届いたコメント")).to_be_visible(timeout=30000)
    assert box.evaluate("el => document.activeElement === el && el.selectionStart") == 2
    assert box.input_value() == "入力中"
    assert not page.errors, page.errors


def test_switching_issue_while_sending(page, remi, fake):
    """送信中に別のチケットへ移っても、移動先の入力欄は「送信中」にならない。"""
    seed(fake)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.locator(".issue", has_text="CSVが文字化けする").click()
    conv = page.locator(".pane-conv")
    playwright_api.expect(conv.get_by_label("ステータス", exact=True)).to_be_enabled(timeout=15000)
    box = conv.get_by_role("textbox", name="コメント")
    with fake._lock:  # 偽Redmineの応答を止めて、送信中の状態を作る
        box.fill("送信中のコメント")
        conv.get_by_role("button", name="送信").click()
        playwright_api.expect(conv.locator(".msg.pending")).to_be_visible()
        page.locator(".issue", has_text="メールの宛先").click()
        playwright_api.expect(conv.locator("h1")).to_have_text("メールの宛先を追加したい")
        playwright_api.expect(conv.locator(".msg.pending")).to_have_count(0)
        box.fill("別のコメント")
        playwright_api.expect(conv.get_by_role("button", name="送信")).to_be_enabled()
    page.locator(".issue", has_text="CSVが文字化けする").click()
    playwright_api.expect(conv.locator(".msg.mine .bubble", has_text="送信中のコメント")).to_be_visible(timeout=20000)
    playwright_api.expect(box).to_have_value("", timeout=10000)
    assert not page.errors, page.errors


def test_tree_toggle_and_settings_tabs(page, remi, fake):
    seed(fake)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    tree = page.locator(".tree")
    playwright_api.expect(tree).to_contain_text("受注管理")
    # 折りたたみは保存を待たずに反映され、←→キーでも操作できる
    page.locator(".node.server .twisty").click()
    playwright_api.expect(tree).not_to_contain_text("未読のみ", timeout=1000)
    page.locator(".node.server .twisty").click()
    playwright_api.expect(tree).to_contain_text("受注管理", timeout=1000)
    page.locator(".node", has_text="基幹システム").click()
    tree.press("ArrowLeft")
    playwright_api.expect(tree).not_to_contain_text("受注管理", timeout=1000)
    tree.press("ArrowRight")
    playwright_api.expect(tree).to_contain_text("受注管理", timeout=1000)
    assert remi.ctx.settings.get("tree_collapsed") == []

    # 設定ダイアログ: タブを切り替えても、変更後の値が表示される
    page.get_by_role("button", name="設定").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("外観").select_option("dark")
    playwright_api.expect(page.locator("html")).to_have_class("theme-dark")
    dialog.get_by_role("tab", name="サーバー").click()
    dialog.get_by_role("tab", name="全般").click()
    playwright_api.expect(dialog.get_by_label("外観")).to_have_value("dark")
    assert not page.errors, page.errors


def test_switching_user_closes_open_conversation(page, remi, fake):
    """別のユーザーのAPIキーに替えたら、開いている会話(前のユーザーのデータ)を閉じる。"""
    seed(fake)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.locator(".issue", has_text="CSVが文字化けする").click()
    conv = page.locator(".pane-conv")
    playwright_api.expect(conv.locator("h1")).to_have_text("CSVが文字化けする")
    conv.get_by_role("textbox", name="コメント").fill("前のユーザーの入力")

    fake.keys["key-b"] = 7
    api = remi.server.api
    probe = api.testConnection(url=fake.base_url, apiKey="key-b", serverId=1)
    api.saveServer({"serverId": 1, "name": "社内Redmine", "url": fake.base_url, "apiKey": "key-b",
                    "textFormat": "common_mark", "pollInterval": 180, "userId": probe["userId"],
                    "login": probe["login"], "userName": probe["name"], "groupIds": probe["groupIds"]})
    playwright_api.expect(conv.locator("h1")).to_have_count(0, timeout=10000)
    playwright_api.expect(conv.get_by_role("textbox", name="コメント")).to_have_count(0)
    assert api.open_issue is None  # 前のユーザーで開いていたチケットを、同期でも取りに行かない
    assert remi.ctx.issues.journals(1, 10) == []
    playwright_api.expect(page.locator(".issue", has_text="CSVが文字化けする")).to_have_count(0, timeout=10000)
    playwright_api.expect(page.locator(".issue", has_text="メールの宛先")).to_be_visible(timeout=20000)
    assert not page.errors, page.errors
