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
    box = conv.get_by_label("コメント")
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
    fake.fail_next = 500
    conv.get_by_label("コメント").fill("失敗するコメント")
    conv.get_by_role("button", name="送信").click()
    playwright_api.expect(conv.get_by_role("alert")).to_contain_text("送信できませんでした", timeout=20000)
    playwright_api.expect(conv.get_by_label("コメント")).to_have_value("失敗するコメント")
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
