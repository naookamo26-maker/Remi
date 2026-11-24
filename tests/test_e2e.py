"""画面のE2Eテスト(設計書 §2.6)。

ローカルAPIサーバーを偽のRedmineにつないで起動し、Playwright(Chromium)で画面を操作する。
Chromium は環境変数 REMI_CHROMIUM、既知の場所、Playwright の既定の順に探す。
"""

from __future__ import annotations

import glob
import os
import re
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
    api = RemiApi(ctx, sync)
    server = LocalServer(api)
    server.start()
    yield type("Remi", (), {"ctx": ctx, "sync": sync, "server": server, "api": api})()
    sync.stop_all()
    server.stop()


@pytest.fixture
def page(browser, remi):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="ja-JP")
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    # 読み込みに失敗したリソースは、どれかが分かるようにURLも残す
    pg.on("console", lambda m: errors.append(f"{m.text} ({m.location.get('url')})") if m.type == "error" else None)
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
    unread_row = page.locator(".node", has_text="未読のみ")
    playwright_api.expect(unread_row.locator(".count-inline")).to_have_text("(1)")
    playwright_api.expect(unread_row.locator(".dot.changed")).to_have_count(1)
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
    page.get_by_role("button", name="設定", exact=True).click()
    dialog = page.get_by_role("dialog", name="設定")
    dialog.get_by_label("外観").select_option("dark")
    playwright_api.expect(page.locator("html")).to_have_class("theme-dark")
    dialog.get_by_label("言語").select_option("en")
    page.wait_for_load_state()
    playwright_api.expect(page.get_by_role("button", name="Settings", exact=True)).to_be_visible(timeout=10000)
    playwright_api.expect(page.locator(".tree")).to_contain_text("Unread only")


def test_project_menu_and_authored_in_the_filter(page, remi, fake):
    seed(fake)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    # 設定は行の右端の歯車から開く(右クリックのメニューには出さない)
    row = page.locator(".tree .node", has_text="基幹システム").first
    row.click(button="right")
    playwright_api.expect(page.get_by_role("menu")).to_contain_text("すべて既読にする")
    playwright_api.expect(page.get_by_role("menu").get_by_role("group", name="通知")).to_have_count(0)
    page.keyboard.press("Escape")
    playwright_api.expect(page.get_by_role("menu")).to_have_count(0)
    row.hover()
    row.locator(".node-gear").click()
    menu = page.get_by_role("menu")
    playwright_api.expect(menu.get_by_role("group", name="通知")).to_be_visible()
    # 「作成したチケットを監視」はなくなり、「表示するチケット」の「自分が作成」に一本化した
    playwright_api.expect(menu.get_by_role("group", name="作成したチケットを監視")).to_have_count(0)
    page.keyboard.press("Escape")
    # 「自分が作成」にチェックすると、作成したチケットも取得する(親で設定すると子にも効く)
    row.hover()
    page.get_by_role("button", name="表示するチケット: 基幹システム").click()
    page.get_by_role("menu").get_by_role("button", name="独自").click()
    menu = page.get_by_role("menu")
    playwright_api.expect(menu.get_by_role("menuitemcheckbox", name="自分が作成")).to_have_attribute("aria-checked", "false")
    menu.get_by_role("menuitemcheckbox", name="自分が作成").click()
    for _ in range(50):
        if "authored" in (remi.ctx.projects.own_visible_relations(1).get(1) or []):
            break
        time.sleep(0.1)
    assert remi.ctx.projects.own_visible_relations(1)[1] == ["assigned", "group", "watching", "authored"]
    assert remi.ctx.projects.projects_showing(1, "authored", None) == {1, 2}


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
    # 「作成」のボタンは、どこかのプロジェクトが「自分が作成」を表示しているときだけ出る(既定では表示しない)
    remi.ctx.servers.update_fields(1, visible_relations=["assigned", "group", "watching", "authored"])
    page.goto(remi.server.start_url)
    playwright_api.expect(page.locator(".issue")).to_have_count(2)
    filters = page.get_by_role("group", name="関係で絞り込み")
    tree_all = page.locator(".tree .node", has_text="すべて").first.locator(".count-inline")
    playwright_api.expect(tree_all).to_have_text("(2)")
    filters.get_by_role("button", name="担当").click()
    playwright_api.expect(page.locator(".issue")).to_have_count(1)
    playwright_api.expect(tree_all).to_have_text("(2)")  # 絞り込みは、真ん中の一覧だけに効く(ツリーの件数は変わらない)
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


def test_redmine_tabs(page, remi, fake, monkeypatch):
    """Redmineをタブで開く。ウィンドウ全体のタブの列(固定の「Remi」とRedmineのタブ)と、操作行。"""
    from remi.server.api import NullWindow

    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    seed(fake)
    add_server(remi, fake)
    # ページを表示する部品がない環境なので、タブの状態だけを動かす窓口に差し替える
    window = NullWindow(simulate_tabs=True)
    window.tabs.on_changed = lambda: remi.ctx.bus.publish("tabsChanged", **window.tabs.snapshot())
    remi.api.window = window
    remi.ctx.settings.set("redmine_open_in", "tab")
    page.goto(remi.server.start_url)
    page.locator(".issue", has_text="CSVが文字化けする").click()
    conv = page.locator(".pane-conv")
    # タブがないときは、タブの列を出さない(これまでと同じ画面)
    playwright_api.expect(page.locator(".tabs-chrome")).to_be_hidden()
    conv.get_by_role("button", name="Remiで開く").click()
    tabs = page.locator(".apptabs .atab")
    playwright_api.expect(tabs).to_have_count(2, timeout=10000)
    playwright_api.expect(tabs.nth(0)).to_have_text("Remi")
    playwright_api.expect(tabs.nth(1)).to_have_attribute("aria-selected", "true")
    # Redmineのタブを選んでいる間は、本体の画面を隠し、操作行を出す(URLは表示のみ)
    assert page.evaluate("() => document.body.classList.contains('browse')")
    playwright_api.expect(page.locator(".layout")).to_be_hidden()
    url = f"{fake.base_url}/issues/10".replace("http://", "")
    playwright_api.expect(page.locator(".tab-tools .tab-url")).to_have_text(url)
    playwright_api.expect(page.locator(".tab-tools input")).to_have_count(0)
    playwright_api.expect(page.get_by_role("button", name="戻る")).to_be_disabled()
    # 固定の「Remi」のタブを選ぶと、本体の画面に戻る
    tabs.nth(0).click()
    playwright_api.expect(page.locator(".layout")).to_be_visible()
    playwright_api.expect(page.locator(".tab-tools")).to_be_hidden()
    # 同じ操作で、もう1つ開ける。Ctrl+W で、選んでいるタブを閉じる
    conv.get_by_role("button", name="Remiで開く").click()
    playwright_api.expect(tabs).to_have_count(3, timeout=10000)
    page.keyboard.press("Control+w")
    playwright_api.expect(tabs).to_have_count(2, timeout=10000)
    # 「別ウィンドウへ」で、タブを閉じて、別のウィンドウで開く
    page.get_by_role("button", name="別ウィンドウへ").click()
    playwright_api.expect(page.locator(".tabs-chrome")).to_be_hidden(timeout=10000)
    assert window.detached == [f"{fake.base_url}/issues/10"] and opened == []
    playwright_api.expect(page.locator(".layout")).to_be_visible()
    # ×で閉じる
    conv.get_by_role("button", name="Remiで開く").click()
    playwright_api.expect(tabs).to_have_count(2, timeout=10000)
    page.get_by_role("button", name="閉じる").click()
    playwright_api.expect(page.locator(".tabs-chrome")).to_be_hidden(timeout=10000)
    assert not page.errors, page.errors


def test_detached_window_chrome_page(page, remi, fake):
    """切り離したウィンドウの上の操作行(chrome.html)。SVGのボタン、URLの表示のみ、ライト/ダークに追従する。"""
    from remi.server.api import NullWindow

    window = NullWindow()
    window.window_states[1] = {"url": "https://rm.example/issues/10", "canBack": True, "canForward": False,
                               "loading": False, "attachable": True}
    remi.api.window = window
    remi.ctx.settings.set("theme", "light")
    page.goto(remi.server.start_url)  # 認証のCookieを受け取る(本物では、本体の画面が受け取ったものを共有する)
    page.goto(f"{remi.server.origin}/chrome.html?w=1")
    bar = page.locator(".tab-tools")
    playwright_api.expect(bar).to_be_visible(timeout=10000)
    playwright_api.expect(bar.locator(".tab-url")).to_have_text("rm.example/issues/10")
    playwright_api.expect(bar.locator("input")).to_have_count(0)  # URLは表示のみ
    # ボタンの記号は文字ではなく、SVGの図形
    assert page.evaluate("() => [...document.querySelectorAll('.tab-tools .nb')].every((b) => b.querySelector('svg') && !b.textContent.trim())")
    playwright_api.expect(page.get_by_role("button", name="戻る")).to_be_enabled()
    playwright_api.expect(page.get_by_role("button", name="進む")).to_be_disabled()
    page.get_by_role("button", name="戻る").click()
    page.get_by_role("button", name="再読み込み").click()
    page.get_by_role("button", name="タブに戻す").click()
    for _ in range(50):
        if len(window.window_actions) >= 3:
            break
        time.sleep(0.1)
    assert window.window_actions == [(1, "back"), (1, "reload"), (1, "attach")]
    # ページの状態が変わると、操作行も変わる
    remi.ctx.bus.publish("windowState", windowId=1, url="https://rm.example/issues/11", canBack=False, canForward=True,
                         loading=False, attachable=False)
    playwright_api.expect(bar.locator(".tab-url")).to_have_text("rm.example/issues/11", timeout=10000)
    playwright_api.expect(page.get_by_role("button", name="進む")).to_be_enabled()
    playwright_api.expect(page.get_by_role("button", name="タブに戻す")).to_have_count(0)
    # アプリの設定でライト/ダークを切り替えると、操作行も追従する
    playwright_api.expect(page.locator("html")).to_have_class(re.compile("theme-light"))
    remi.api.setSetting("theme", "dark")
    playwright_api.expect(page.locator("html")).to_have_class(re.compile("theme-dark"), timeout=10000)
    assert not page.errors, page.errors


def test_tree_hides_all_and_unread_rows_when_nothing_is_shown(page, remi, fake):
    """表示するチケットがなければ、プロジェクトが出ていても(「すべてを表示」)、「すべて」「未読のみ」は出さない。"""
    seed(fake)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    tree = page.locator(".tree")
    playwright_api.expect(tree).to_contain_text("未読のみ", timeout=20000)
    # すべてのフィルタを外し、チケットのないプロジェクトも出す
    remi.ctx.servers.update_fields(1, visible_relations=[], show_all_projects=True)
    remi.ctx.bus.publish("projectSettingsChanged", serverId=1)
    playwright_api.expect(tree).not_to_contain_text("未読のみ", timeout=10000)
    playwright_api.expect(tree).to_contain_text("基幹システム")
    assert tree.locator(".node", has_text="すべて").filter(has_not_text="すべてを表示").count() == 0
    # 戻すと、また出る
    remi.ctx.servers.update_fields(1, visible_relations=None)
    remi.ctx.bus.publish("projectSettingsChanged", serverId=1)
    playwright_api.expect(tree).to_contain_text("未読のみ", timeout=10000)
    assert not page.errors, page.errors


def test_overlays_are_hidden_while_a_redmine_tab_is_selected(page, remi, fake):
    """タブを選んでいる間は、本体の画面が細くなるので、ダイアログなどの重ねて表示する要素は隠す(残骸を出さない)。"""
    from remi.server.api import NullWindow

    seed(fake)
    add_server(remi, fake)
    window = NullWindow(simulate_tabs=True)
    window.tabs.on_changed = lambda: remi.ctx.bus.publish("tabsChanged", **window.tabs.snapshot())
    remi.api.window = window
    page.goto(remi.server.start_url)
    playwright_api.expect(page.locator(".issue").first).to_be_visible(timeout=20000)
    page.get_by_role("button", name="設定", exact=True).click()
    dialog = page.get_by_role("dialog")
    playwright_api.expect(dialog).to_be_visible()
    # 切り離したウィンドウの「タブに戻す」などで、設定を開いたままタブが開かれた
    window.tabs.open(f"{fake.base_url}/issues/10")
    playwright_api.expect(page.locator(".atab")).to_have_count(2, timeout=10000)
    playwright_api.expect(page.locator(".backdrop")).to_be_hidden()
    playwright_api.expect(dialog).to_be_hidden()
    # 通知(トースト)も、細くなった画面に出さない
    page.evaluate("() => { const d = document.createElement('div'); d.className = 'toast'; d.textContent = 'x'; document.body.append(d); }")
    playwright_api.expect(page.locator(".toast")).to_be_hidden()
    # 「Remi」のタブに戻ると、そのまま戻る
    page.locator(".atab.fixed").click()
    playwright_api.expect(dialog).to_be_visible(timeout=10000)
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
    conv.get_by_role("button", name="Remiで開く").click()
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
    page.get_by_role("button", name="設定", exact=True).click()
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


def test_f5_refetches_open_issue_without_reloading(page, remi, fake):
    """F5・Ctrl+R は画面を再読み込みせず、開いているチケットのコメントをRedmineから取り直す。"""
    seed(fake)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.locator(".issue", has_text="CSVが文字化けする").click()
    conv = page.locator(".pane-conv")
    playwright_api.expect(conv.locator("h1")).to_have_text("CSVが文字化けする")
    time.sleep(2)  # 開いた直後の取り直しが終わるのを待つ
    page.evaluate("window.__notReloaded = true")
    # 同じ秒に入った(更新日時が変わらない)コメントは、通常の確認では見つからない
    stamp = remi.ctx.issues.updated_on(1, 10)
    fake.add_journal(10, 8, "見逃したコメント", created_on=stamp)
    page.keyboard.press("F5")
    playwright_api.expect(conv.locator(".bubble", has_text="見逃したコメント")).to_be_visible(timeout=8000)
    page.keyboard.press("Control+r")
    page.keyboard.press("Control+Shift+R")
    time.sleep(0.5)
    assert page.evaluate("window.__notReloaded") is True
    assert not page.errors, page.errors


def test_list_keys_search_shortcut_and_lightbox(page, remi, fake):
    seed(fake)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    playwright_api.expect(page.locator(".issue")).to_have_count(2, timeout=20000)
    # Ctrl+F で検索欄へ。Esc で絞り込みを解除
    page.locator(".tree").click()
    page.keyboard.press("Control+f")
    search = page.get_by_role("searchbox", name="チケットを検索")
    playwright_api.expect(search).to_be_focused()
    search.press_sequentially("メール")
    playwright_api.expect(page.locator(".issue")).to_have_count(1)
    search.press("Escape")
    playwright_api.expect(search).to_have_value("")
    playwright_api.expect(page.locator(".issue")).to_have_count(2)
    # Home / End で先頭・末尾のチケットを開く
    search.press("ArrowDown")
    page.keyboard.press("End")
    playwright_api.expect(page.locator(".issue").last).to_have_class(re.compile("selected"))
    page.keyboard.press("Home")
    playwright_api.expect(page.locator(".issue").first).to_have_class(re.compile("selected"))

    # 画像の拡大表示: 閉じるボタンにフォーカスがあり、Tabで外へ出ず、閉じると元の場所へ戻る
    page.locator(".issue", has_text="CSVが文字化けする").click()
    img = page.locator(".pane-conv .bubble img")
    img.click()
    box = page.get_by_role("dialog", name="画面")
    close = box.get_by_role("button", name="閉じる")
    playwright_api.expect(close).to_be_focused()
    page.keyboard.press("Tab")
    playwright_api.expect(close).to_be_focused()
    page.keyboard.press("Enter")
    playwright_api.expect(box).to_have_count(0)
    assert not page.errors, page.errors


def test_pane_widths_change_only_by_splitter(page, remi, fake):
    seed(fake)
    # 長い行(折り返せないコード)のあるチケット: 内容でペインの幅が押し縮められないこと
    fake.add_journal(11, 7, "```\n" + "x" * 400 + "\n```")
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    playwright_api.expect(page.locator(".issue")).to_have_count(2, timeout=20000)
    widths = """() => ['.pane-tree', '.pane-list'].map((s) => Math.round(document.querySelector(s).getBoundingClientRect().width))"""
    before = page.evaluate(widths)
    for name in ("CSVが文字化けする", "メールの宛先", "CSVが文字化けする"):
        page.locator(".issue", has_text=name).click()
        playwright_api.expect(page.locator(".pane-conv h1")).to_contain_text(name)
        assert page.evaluate(widths) == before
    # 一覧と会話の間の仕切りを動かしても、ツリーの幅は変わらない
    box = page.locator(".splitter").nth(1).bounding_box()
    page.mouse.move(box["x"] + 2, box["y"] + 200)
    page.mouse.down()
    page.mouse.move(box["x"] + 200, box["y"] + 200, steps=5)
    page.mouse.up()
    after = page.evaluate(widths)
    assert after[0] == before[0] and after[1] > before[1]
    assert not page.errors, page.errors


def test_header_buttons_project_relations_and_grouped_changes(page, remi, fake):
    seed(fake)
    fake.add_journal(10, 7, details=[
        {"property": "attr", "name": "status_id", "old_value": "1", "new_value": "2"},
        {"property": "attr", "name": "priority_id", "old_value": "2", "new_value": "3"},
    ])
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    playwright_api.expect(page.locator(".issue")).to_have_count(2, timeout=20000)
    # バージョンはウィンドウのタイトルに「v」付きで出す(ツリーの上には出さない)
    assert re.fullmatch(r"Remi v\d+\.\d+\.\d+.*", page.title())
    playwright_api.expect(page.locator(".pane-tree")).not_to_contain_text("Remi")
    # サーバーの追加は、設定を開かずにツリーの上のボタンから直接開ける
    page.locator(".tree-header").get_by_role("button", name="サーバーを追加").click()
    dialog = page.get_by_role("dialog", name="サーバーの登録")
    playwright_api.expect(dialog).to_be_visible()
    dialog.get_by_role("button", name="キャンセル").click()
    # 表示するチケット(プロジェクトごと): 基幹システムでウォッチ中を外すと、子プロジェクトのウォッチだけの
    # チケットも、一覧とツリーの件数から外れる
    page.get_by_role("button", name="表示するチケット: 基幹システム").click()
    menu = page.get_by_role("menu")
    # 最上位のプロジェクトは、既定ではサーバーの既定を引き継ぐ。そのあいだはチェックを変えられない
    playwright_api.expect(menu.get_by_role("button", name="サーバーと同じ")).to_have_attribute("aria-pressed", "true")
    playwright_api.expect(menu.get_by_role("menuitemcheckbox", name="ウォッチ中")).to_be_disabled()
    menu.get_by_role("button", name="独自").click()
    menu = page.get_by_role("menu")
    playwright_api.expect(menu.get_by_role("button", name="独自")).to_have_attribute("aria-pressed", "true")
    menu.get_by_role("menuitemcheckbox", name="ウォッチ中").click()
    playwright_api.expect(page.locator(".issue")).to_have_count(1)
    menu = page.get_by_role("menu")
    playwright_api.expect(menu.get_by_role("menuitemcheckbox", name="ウォッチ中")).to_have_attribute("aria-checked", "false")
    playwright_api.expect(page.locator(".tree .node", has_text="すべて").locator(".count-inline")).to_have_text("(1)")
    assert remi.ctx.projects.own_visible_relations(1) == {1: ["assigned", "group"]}
    # 子プロジェクトは親の設定を継承していることを示す(担当のチケットが残るので、ツリーにも残る)
    playwright_api.expect(page.get_by_role("button", name="表示するチケット: 受注管理")).to_have_class(re.compile("inherited"))
    playwright_api.expect(page.get_by_role("button", name="表示するチケット: 基幹システム")).to_have_class(re.compile("own"))
    # サーバーと同じに戻すと、独自の設定は消える
    menu.get_by_role("button", name="サーバーと同じ").click()
    playwright_api.expect(page.locator(".issue")).to_have_count(2)
    playwright_api.expect(page.locator(".tree .node", has_text="すべて").locator(".count-inline")).to_have_text("(2)")
    page.keyboard.press("Escape")
    assert remi.ctx.projects.own_visible_relations(1) == {}
    # サーバーの右クリックメニューは、操作だけ(「すべてのプロジェクトを表示」や「表示するチケット」の設定は置かない)
    page.locator(".tree .node.server").click(button="right")
    menu = page.get_by_role("menu")
    playwright_api.expect(menu).to_contain_text("すべて既読にする")
    playwright_api.expect(menu).not_to_contain_text("すべてのプロジェクトを表示")
    playwright_api.expect(menu).not_to_contain_text("表示するチケット")
    page.keyboard.press("Escape")
    # 「すべてのプロジェクトを表示」は、サーバーごとの設定(ふだんは、表示するものがないプロジェクトがあるときの
    # ツリーのリンクから切り替える)。ここではそのリンクが出るまでもなく、設定を直接オンにする
    remi.ctx.servers.update_fields(1, show_all_projects=True)
    remi.ctx.bus.publish("projectSettingsChanged", serverId=1)
    # サーバーの下のリンクで、隠すことも戻すこともできる
    link = page.locator(".tree .node.link-row")
    playwright_api.expect(link).to_have_text("非表示に戻す")
    # ふだんから出ているプロジェクトは、薄い色にならない(「すべてを表示」でだけ出るプロジェクトが薄くなるのは、
    # サーバーのテストで確かめている。ここのデータには、チケットのないプロジェクトがない)
    playwright_api.expect(page.locator(".tree .node.dim")).to_have_count(0)
    link.click()
    end = time.monotonic() + 5
    while time.monotonic() < end and remi.ctx.servers.get(1).show_all_projects:
        time.sleep(0.05)
    assert remi.ctx.servers.get(1).show_all_projects is False
    # 1回の更新での複数の変更は、1つにまとめて表示する
    page.locator(".issue", has_text="CSVが文字化けする").click()
    group = page.locator(".pane-conv .change-group")
    playwright_api.expect(group).to_have_count(1, timeout=15000)
    playwright_api.expect(group.locator("li")).to_have_count(2)
    assert not page.errors, page.errors


def test_selecting_tree_item_closes_conversation_and_keeps_draft(page, remi, fake):
    seed(fake)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.locator(".issue", has_text="CSVが文字化けする").click()
    conv = page.locator(".pane-conv")
    playwright_api.expect(conv.locator("h1")).to_contain_text("CSVが文字化けする", timeout=15000)
    conv.get_by_role("textbox", name="コメント").fill("書きかけ")
    # すぐに別の項目を選ぶ: 会話は閉じ、書きかけの文章は下書きとして残る
    page.locator(".tree .node", has_text="受注管理").click()
    playwright_api.expect(conv.locator("h1")).to_have_count(0)
    playwright_api.expect(conv).to_contain_text("チケットを選ぶと会話が表示されます")
    end = time.monotonic() + 5
    while time.monotonic() < end and remi.ctx.issues.get_draft(1, 10)[0] != "書きかけ":
        time.sleep(0.05)
    assert remi.ctx.issues.get_draft(1, 10)[0] == "書きかけ"
    page.locator(".issue", has_text="CSVが文字化けする").click()
    playwright_api.expect(conv.get_by_role("textbox", name="コメント")).to_have_value("書きかけ", timeout=15000)
    assert not page.errors, page.errors


def test_server_default_shows_milestones(page, remi, fake):
    """サーバーの⚙ダイアログで「マイルストーンを表示する」をオンにすると、継承しているプロジェクトに効く。"""
    seed(fake)
    fake.add_version(5, 2, "v1.0", due_date="2026-12-01")
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    playwright_api.expect(page.locator(".issue")).to_have_count(2, timeout=20000)
    playwright_api.expect(page.locator(".tree .node.version")).to_have_count(0)
    row = page.locator(".tree .node.server")
    row.hover()
    row.locator(".node-gear").click()
    page.get_by_label("マイルストーンを表示する(プロジェクトの設定が「継承」のときの既定)").check()
    page.get_by_role("button", name="保存").click()
    playwright_api.expect(page.locator(".tree .node.version", has_text="v1.0")).to_be_visible(timeout=20000)
    assert remi.ctx.servers.get(1).watch_versions is True
    assert not page.errors, page.errors


def test_roadmap_versions(page, remi, fake):
    seed(fake)
    fake.add_version(5, 2, "v1.0", due_date="2026-12-01")
    fake.add_version(6, 2, "v0.9", status="closed")
    fake.add_issue(20, 2, "関係ないチケット", assigned_to=7, version=5)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    playwright_api.expect(page.locator(".issue")).to_have_count(2, timeout=20000)
    # プロジェクトの歯車のメニューで「マイルストーンを表示する」をオンにする
    row = page.locator(".tree .node", has_text="基幹システム").first
    row.hover()
    row.locator(".node-gear").click()
    page.get_by_role("group", name="マイルストーンを表示する").get_by_role("button", name="オン").click()
    version = page.locator(".tree .node.version", has_text="v1.0")
    playwright_api.expect(version).to_be_visible(timeout=20000)
    # 子プロジェクトのないプロジェクトでは、「ロードマップ」の行を出さず、バージョンを直下に並べる
    playwright_api.expect(page.locator(".tree .node.roadmap")).to_have_count(0)
    # 旗は、開閉の ▼ と同じ場所に出す
    playwright_api.expect(version.locator(".twisty svg")).to_have_count(1)
    # 終了したバージョンは畳んでおく
    playwright_api.expect(page.locator(".tree .node.version", has_text="v0.9")).to_have_count(0)
    page.locator(".tree .node.version-group").click()
    closed = page.locator(".tree .node.version", has_text="v0.9")
    playwright_api.expect(closed).to_be_visible()
    # 終了したバージョンのチケットは、開いたときに初めて取得する(それまでは件数を「…」で出す)
    playwright_api.expect(closed.locator(".count-inline")).to_have_text("(…)")
    closed.click()
    playwright_api.expect(closed.locator(".count-inline")).to_have_text("(0/0)", timeout=20000)
    # バージョンを選ぶと、関係ないチケットも一覧に出て、右に概要が出る(ほかの一覧の件数は変わらない)
    version.click()
    playwright_api.expect(page.locator(".issue")).to_have_count(1)
    playwright_api.expect(page.locator(".issue")).to_contain_text("関係ないチケット")
    conv = page.locator(".pane-conv")
    playwright_api.expect(conv.locator("h1")).to_contain_text("v1.0")
    playwright_api.expect(conv).to_contain_text("まだ変化はありません")
    # 変化が届くと、表示中のバージョンの記録に出る
    fake.add_issue(21, 2, "追加されたチケット", assigned_to=7)
    fake.set_version(21, 5)
    remi.sync.sync_now()
    playwright_api.expect(conv.locator(".version-event.added")).to_contain_text("#21", timeout=20000)
    # チケットを開いてから、同じバージョンをもう一度押すと、変化の記録に戻る
    page.locator(".issue", has_text="関係ないチケット").click()
    playwright_api.expect(conv.locator("h1")).to_contain_text("関係ないチケット", timeout=15000)
    version.click()
    playwright_api.expect(conv.locator("h1")).to_contain_text("v1.0")
    # 別の項目を選ぶと閉じる。プロジェクトを畳んでも、中のバージョンに変化があれば、親に青い丸が出る
    page.locator(".tree .node", has_text="すべて").first.click()
    playwright_api.expect(conv.locator("h1")).to_have_count(0)
    project = page.locator(".tree .node", has_text="受注管理").first
    project.locator(".twisty").click()
    playwright_api.expect(version).to_have_count(0)
    fake.set_version(20, None)
    remi.sync.sync_now()
    playwright_api.expect(project.locator(".dot.changed")).to_be_visible(timeout=20000)
    # 開くと、丸は実際の場所(バージョン)に移る
    project.locator(".twisty").click()
    playwright_api.expect(project.locator(".dot.changed")).to_have_count(0)
    playwright_api.expect(version.locator(".dot.changed")).to_be_visible()
    assert not page.errors, page.errors


def test_issue_details_fields(page, remi, fake):
    seed(fake)
    fake.categories[2] = [{"id": 3, "name": "帳票"}]
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.locator(".issue", has_text="CSVが文字化けする").click()
    conv = page.locator(".pane-conv")
    playwright_api.expect(conv.locator("h1")).to_contain_text("CSVが文字化けする", timeout=15000)
    # 既定では閉じている(ヘッダは3項目だけ)
    playwright_api.expect(conv.locator(".details-fields")).to_be_hidden()
    conv.get_by_role("button", name="詳細").click()
    category = conv.get_by_label("カテゴリ")
    playwright_api.expect(category).to_be_enabled(timeout=15000)
    category.select_option(label="帳票")
    conv.get_by_label("期日").fill("2026-11-30")
    chips = conv.locator(".chip")
    playwright_api.expect(chips).to_have_count(2)
    playwright_api.expect(chips.first).to_contain_text("カテゴリ")
    conv.get_by_role("button", name="送信").click()
    end = time.monotonic() + 15
    while time.monotonic() < end and fake.issues[10].get("due_date") != "2026-11-30":
        time.sleep(0.1)
    assert fake.issues[10]["category"]["id"] == 3 and fake.issues[10]["due_date"] == "2026-11-30"
    playwright_api.expect(chips).to_have_count(0)
    # 開いた状態は覚えておく(別のチケットを開いても開いたまま)
    assert remi.ctx.settings.get("issue_details_open") is True
    page.locator(".issue", has_text="メールの宛先").click()
    playwright_api.expect(conv.locator("h1")).to_contain_text("メールの宛先", timeout=15000)
    playwright_api.expect(conv.locator(".details-fields")).to_be_visible()
    assert not page.errors, page.errors


def test_custom_field_link_button(page, remi, fake, monkeypatch):
    """文字列のカスタムフィールドの値がリンクなら、右に「リンクを開く」ボタンを出す(入力欄はそのまま)。"""
    import remi.server.api as api_module

    opened = []
    monkeypatch.setattr(api_module.webbrowser, "open", opened.append)
    seed(fake)
    fake.admin = True
    fake.custom_fields = [{"id": 1, "name": "仕様書", "field_format": "string"},
                          {"id": 2, "name": "顧客", "field_format": "string"}]
    fake.issues[10]["custom_fields"] = [{"id": 1, "name": "仕様書", "value": "https://wiki.example.com/spec"},
                                        {"id": 2, "name": "顧客", "value": "山田商事"}]
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.locator(".issue", has_text="CSVが文字化けする").click()
    conv = page.locator(".pane-conv")
    playwright_api.expect(conv.locator("h1")).to_contain_text("CSVが文字化けする", timeout=15000)
    conv.get_by_role("button", name="詳細").click()
    spec = conv.locator("label.detail", has_text="仕様書")
    customer = conv.locator("label.detail", has_text="顧客")
    playwright_api.expect(spec.get_by_label("仕様書")).to_be_enabled(timeout=15000)
    playwright_api.expect(spec.get_by_role("button", name="リンクを開く")).to_be_visible()
    playwright_api.expect(customer.get_by_role("button", name="リンクを開く")).to_be_hidden()
    # 入力に合わせて、出たり消えたりする(入力欄にフォーカスしたまま)
    customer.get_by_label("顧客").fill("www.example.co.jp")
    playwright_api.expect(customer.get_by_role("button", name="リンクを開く")).to_be_visible()
    spec.get_by_label("仕様書").fill("仕様は別紙")
    playwright_api.expect(spec.get_by_role("button", name="リンクを開く")).to_be_hidden()
    # 押すと、既定のブラウザで開く(www. は https を補う)
    customer.get_by_role("button", name="リンクを開く").click()
    end = time.monotonic() + 5
    while time.monotonic() < end and not opened:
        time.sleep(0.05)
    assert opened == ["https://www.example.co.jp"]
    assert not page.errors, page.errors


def test_edit_delete_note_attachment_and_uri_link(page, remi, fake, monkeypatch):
    import remi.server.api as api_module

    opened = []
    monkeypatch.setattr(api_module, "open_with_os", opened.append)
    seed(fake)
    mine = fake.add_journal(10, ME, "自分のコメント。共有は smb://fs01/share です")
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.locator(".issue", has_text="CSVが文字化けする").click()
    conv = page.locator(".pane-conv")
    msg = conv.locator(".msg", has_text="自分のコメント")
    playwright_api.expect(msg).to_be_visible(timeout=15000)

    # 自動リンク: 押すと確認してから、関連付けられたアプリで開く
    msg.get_by_role("link", name="smb://fs01/share").click()
    page.get_by_role("dialog", name="リンクを開く").get_by_role("button", name="開く").click()
    end = time.monotonic() + 5
    while time.monotonic() < end and not opened:
        time.sleep(0.05)
    assert opened == ["smb://fs01/share"]

    # 編集: 吹き出しの「⋯」から
    msg.hover()
    msg.get_by_role("button", name="コメントの操作").click()
    page.get_by_role("menuitem", name="編集").click()
    editor = conv.locator(".note-editor textarea")
    editor.fill("書き直したコメント")
    conv.locator(".note-editor").get_by_role("button", name="保存").click()
    playwright_api.expect(conv.locator(".msg", has_text="書き直したコメント")).to_be_visible(timeout=15000)
    assert next(j for j in fake.journals[10] if j["id"] == mine["id"])["notes"] == "書き直したコメント"

    # 削除
    msg = conv.locator(".msg", has_text="書き直したコメント")
    msg.hover()
    msg.get_by_role("button", name="コメントの操作").click()
    page.get_by_role("menuitem", name="コメントを削除").click()
    page.get_by_role("dialog", name="コメントを削除").get_by_role("button", name="削除").click()
    playwright_api.expect(conv.locator(".msg", has_text="書き直したコメント")).to_have_count(0, timeout=15000)

    # 添付の削除(添付の行の「⋯」メニューから)
    fake.add_journal(10, 7, details=[{"property": "attachment", "name": "77", "new_value": "shot.png"}])
    remi.sync.refetch_issue(1, 10)
    tools = conv.locator(".change-group, .system").get_by_role("button", name="ファイルの操作").first
    playwright_api.expect(tools).to_be_attached(timeout=15000)
    tools.evaluate("el => el.closest('li, .system').dispatchEvent(new MouseEvent('mouseover', { bubbles: true }))")
    tools.click()
    page.get_by_role("menuitem", name="ファイルを削除").click()
    page.get_by_role("dialog", name="ファイルを削除").get_by_role("button", name="削除").click()
    end = time.monotonic() + 15
    while time.monotonic() < end and 77 in fake.attachments:
        time.sleep(0.1)
    assert 77 not in fake.attachments
    playwright_api.expect(conv.get_by_role("button", name="ファイルの操作")).to_have_count(0, timeout=15000)
    # 削除した添付の画像は、削除と同時に描き直されると、一瞬だけ読み込みに失敗することがある(次の描き直しで消える)
    assert not [e for e in page.errors if "/attachments/1/77" not in e], page.errors


# --- 全体の確認(レビュー)で決めた使い勝手の改善 ---------------------------------------------
def _open_app(page, remi, fake):
    seed(fake)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.wait_for_selector(".issue-list li.issue")


def test_list_order_stays_while_reading_with_arrow_keys(page, remi, fake):
    """↓で未読を順に読んでも、既読になったチケットが下へ移って、次の未読を飛ばすことがない。"""
    seed(fake)
    fake.add_issue(12, 2, "三つ目", watchers={ME})
    fake.add_journal(11, 7, "二つ目の新着")
    fake.add_journal(12, 7, "三つ目の新着")
    add_server(remi, fake)
    end = time.monotonic() + 20
    while time.monotonic() < end and not remi.ctx.issues.journals_synced(1, 12):
        time.sleep(0.1)
    for i in (10, 11, 12):
        remi.ctx.issues.mark_unread(1, i)
    page.goto(remi.server.start_url)
    page.wait_for_selector(".issue-list li.issue.unread")
    unread = [li.get_attribute("data-issue") for li in page.locator(".issue-list li.issue.unread").all()]
    assert len(unread) == 3
    page.locator(".issue-list li.issue").first.click()
    page.locator(".issue-list").focus()
    opened = [page.evaluate("() => window.remi.current.issueId")]
    for _ in range(2):
        page.keyboard.press("ArrowDown")
        page.wait_for_function("(prev) => window.remi.current.issueId !== prev", arg=opened[-1])
        page.wait_for_timeout(700)  # 既読になって、一覧が更新されるのを待つ
        opened.append(page.evaluate("() => window.remi.current.issueId"))
    assert sorted(map(str, opened)) == sorted(unread)  # 3件とも、飛ばさずに開けた
    page.evaluate("() => window.remi.closeIssue()")  # 会話を閉じたら、既読になったものが下へ移る
    assert page.locator(".issue-list li.issue.unread").count() == 0


def test_arrow_keys_work_right_after_selecting_with_the_mouse(page, remi, fake):
    """別の場所にフォーカスがあっても、マウスでチケットを選んだら、そのまま上下キーで移動できる(明示的な focus() なし)。"""
    seed(fake)
    fake.add_journal(11, 7, "二つ目")
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.wait_for_selector(".issue-list li.issue")
    page.locator(".issue-list li.issue").nth(1).click()
    page.wait_for_function("() => window.remi.current")
    first = page.evaluate("() => window.remi.current.issueId")
    page.keyboard.press("ArrowUp")
    page.wait_for_function("(prev) => window.remi.current.issueId !== prev", arg=first)


def test_new_messages_button_when_scrolled_up(page, remi, fake):
    seed(fake)
    for n in range(30):
        fake.add_journal(10, 7, f"長い会話のコメント{n}\n\n" + "行\n" * 6)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.locator(".issue-list li.issue", has_text="#10").click()
    page.wait_for_selector(".timeline .msg")
    page.wait_for_timeout(800)
    page.evaluate("document.querySelector('.timeline').scrollTop = 0")
    page.wait_for_timeout(200)
    fake.add_journal(10, 7, "上を読んでいる間の新着")
    fake.issues[10]["updated_on"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 5))
    remi.sync.sync_now()
    btn = page.locator(".timeline .new-messages")
    btn.wait_for(timeout=15000)
    assert "新着 1 件" in btn.inner_text()
    assert remi.ctx.issues.unread_count(1, 10) == 1  # 最下部まで見るまでは、既読にしない
    btn.click()
    page.wait_for_function("() => !document.querySelector('.timeline .new-messages')")
    time.sleep(0.8)
    assert remi.ctx.issues.unread_count(1, 10) == 0


def test_dialogs_keep_input_and_escape_closes_only_the_top_one(page, remi, fake):
    _open_app(page, remi, fake)
    page.evaluate("window.remi.openSettings()")
    page.evaluate("window.remi.openServerDialog()")
    page.locator(".dialog input[type=url]").fill("https://x.example.com")
    page.mouse.click(5, 400)  # 外側をクリックしても、入力の途中のダイアログは閉じない
    assert page.locator(".backdrop").count() == 2
    page.keyboard.press("Escape")  # Escは手前の1つだけ
    assert page.locator(".backdrop").count() == 1
    page.keyboard.press("Escape")
    assert page.locator(".backdrop").count() == 0


def test_sync_button_spins_and_second_press_is_refused(page, remi, fake):
    _open_app(page, remi, fake)
    fake.delay = 0.6
    page.keyboard.press("F5")
    page.wait_for_selector(".tree-header .icon-only .spin")
    page.keyboard.press("F5")
    page.wait_for_selector(".toast:has-text('同期中です')")
    page.wait_for_function("() => !document.querySelector('.tree-header .icon-only .spin')", timeout=20000)


def test_row_more_button_and_menu_names(page, remi, fake):
    _open_app(page, remi, fake)
    row = page.locator(".issue-list li.issue").first
    row.hover()
    row.get_by_role("button", name="操作").click()
    assert page.get_by_role("menuitem", name="Remiで開く").count() == 1
    assert page.get_by_role("menuitem", name="外部ブラウザで開く").count() == 1
    page.keyboard.press("Escape")
    remi.ctx.settings.set("redmine_open_in", "browser")
    page.reload()
    page.wait_for_selector(".issue-list li.issue")
    page.locator(".issue-list li.issue").first.click(button="right")
    assert page.get_by_role("menuitem", name="Remiで開く").count() == 0  # 設定が外部ブラウザなら、1つだけ
    assert page.get_by_role("menuitem", name="外部ブラウザで開く").count() == 1


def test_assignee_shows_my_name_not_the_action(page, remi, fake):
    seed(fake)
    fake.add_issue(12, 2, "自分が担当", assigned_to=ME)
    fake.members[2] = [ME, 7, 8]
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.locator(".issue-list li.issue", has_text="#12").click()
    page.wait_for_selector(".conv-header select:not([disabled])")
    shown = page.locator(".conv-header select").nth(1).evaluate("e => e.selectedOptions[0].textContent")
    assert shown == "岡本 ナオ"


def test_load_failure_shows_error_and_retry(page, remi, fake):
    """会話の読み込みに失敗したら、読み込み中のままにせず、原因と「もう一度読み込む」を出す。押すと開き直せる。"""
    seed(fake)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.wait_for_selector(".issue-list li.issue")
    page.evaluate("""() => { const orig = window.fetch.bind(window); window.__fail = true;
      window.fetch = async (u, o) => { if (window.__fail && String(u).includes('getConversation')) {
        await new Promise((r) => setTimeout(r, 400));
        return new Response(JSON.stringify({ error: { code: 'connection_failed' } }), { status: 200 }); }
        return orig(u, o); }; }""")
    page.locator(".issue-list li.issue", has_text="#10").click()
    page.wait_for_selector(".load-error")
    assert "会話を読み込めませんでした" in page.locator(".load-error").inner_text()
    assert page.locator(".pane-conv .spin").count() == 0  # 読み込み中の表示は残らない
    page.evaluate("() => { window.__fail = false; }")
    page.get_by_role("button", name="もう一度読み込む").click()
    page.wait_for_selector(".conv-header")
    assert "CSVが文字化けする" in page.locator(".conv-header h1").inner_text()


def test_stale_conversation_response_does_not_overwrite_newer(page, remi, fake):
    seed(fake)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.wait_for_selector(".issue-list li.issue")
    page.evaluate("""() => { const orig = window.fetch.bind(window); let n = 0;
      window.fetch = async (u, o) => { const k = String(u).includes('getConversation') ? ++n : 0;
        const r = await orig(u, o); if (k === 1) await new Promise((res) => setTimeout(res, 1500)); return r; }; }""")
    page.locator(".issue-list li.issue", has_text="#10").click()
    page.wait_for_timeout(400)
    remi.ctx.db.query("UPDATE issues SET subject = '新しい件名' WHERE issue_id = 10")
    page.evaluate("() => window.remi.conv.load(1, 10, { markRead: false })")  # あとから始めた読み込み(新しい内容)
    page.wait_for_timeout(2500)  # 遅れた古い応答が届いたあと
    assert page.locator(".conv-header h1").inner_text() == "新しい件名"


def test_late_not_found_does_not_wipe_other_view(page, remi, fake):
    """チケットを開く途中で閉じたあとに「見つからない」が届いても、いまの表示を消さない。"""
    seed(fake)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.wait_for_selector(".issue-list li.issue")
    page.evaluate("""() => { const orig = window.fetch.bind(window);
      window.fetch = async (u, o) => { if (String(u).includes('getConversation')) {
        await new Promise((r) => setTimeout(r, 1200));
        return new Response(JSON.stringify({ error: { code: 'not_found' } }), { status: 200 }); }
        return orig(u, o); }; }""")
    page.locator(".issue-list li.issue", has_text="#10").click()
    page.wait_for_timeout(100)
    page.evaluate("""() => { window.remi.closeIssue();
      window.remi.conv.root.replaceChildren(Object.assign(document.createElement('div'),
        { id: 'other-view', textContent: '別の画面' })); }""")
    page.wait_for_timeout(2000)
    assert page.locator("#other-view").count() == 1
    assert page.locator(".pane-conv .spin").count() == 0  # 読み込み中の表示のタイマーも、閉じたあとは効かない


def test_draft_save_status(page, remi, fake):
    """下書きの保存状態を、入力欄の近くに出す。保存済みは消え、失敗のときは再保存できる(設計書 §6.1)。"""
    seed(fake)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.locator(".issue", has_text="CSVが文字化けする").click()
    state = page.locator(".composer .draft-state")
    playwright_api.expect(state).to_be_hidden()  # 通常は何も出さない
    assert "下書きは自動保存されます" not in page.locator(".composer").inner_text()

    page.locator(".composer textarea").fill("途中の文章")
    playwright_api.expect(state).to_contain_text("保存待ち")
    playwright_api.expect(state).to_contain_text("下書き保存済み")
    playwright_api.expect(state).to_be_hidden(timeout=5000)  # しばらくすると消える
    assert remi.ctx.issues.get_draft(1, 10)[0] == "途中の文章"

    # 保存に失敗したら、そう出して、再保存できる
    page.route("**/api/saveDraft", lambda route: route.abort())
    page.locator(".composer textarea").fill("失敗する文章")
    playwright_api.expect(state).to_contain_text("下書きを保存できませんでした")
    page.unroute("**/api/saveDraft")
    page.get_by_role("button", name="再保存").click()
    playwright_api.expect(state).to_contain_text("下書き保存済み")
    assert remi.ctx.issues.get_draft(1, 10)[0] == "失敗する文章"

    # 本文を空にすると下書きは残らないので、「保存済み」とは出さない
    page.locator(".composer textarea").fill("")
    page.wait_for_timeout(1200)
    playwright_api.expect(state).to_be_hidden()
    # 失敗させた通信のログ以外に、エラーはない
    assert not [e for e in page.errors if "api/saveDraft" not in e], page.errors


def _open_list(page, remi, fake, closed=True):
    """担当・ウォッチのチケットと、クローズ済みの担当チケット(12)を用意して、一覧を開く。"""
    seed(fake)
    if closed:
        fake.add_issue(12, 2, "終了済みの件", status_id=5)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.wait_for_selector(".issue-list li.issue")


def test_search_popup_and_chip(page, remi, fake):
    """🔍を押すと小さなウィンドウで検索できる。検索語はチップに残り、✕で消せる。保存して次回も使う(設計書 §5.1)。"""
    _open_list(page, remi, fake)
    chips = page.locator(".cchips")
    playwright_api.expect(chips).to_be_hidden()  # 条件がなければ、チップの行は出ない
    assert page.locator(".search input").count() == 0  # 検索ボックスは、押したときだけ出る

    page.get_by_role("button", name="検索 (Ctrl+F)").click()
    box = page.locator(".search-pop input")
    playwright_api.expect(box).to_be_focused()
    box.fill("CSV")
    playwright_api.expect(page.locator(".issue")).to_have_count(1)
    page.keyboard.press("Enter")  # 閉じても、検索語はチップとして残る
    playwright_api.expect(page.locator(".search-pop")).to_have_count(0)
    playwright_api.expect(chips).to_contain_text("検索「CSV」")
    playwright_api.expect(page.get_by_role("button", name="検索 (Ctrl+F)")).to_have_attribute("aria-pressed", "true")
    assert remi.ctx.settings.get("list_search") == "CSV"

    page.reload()  # 次に開いたときも使う
    page.wait_for_selector(".issue-list li.issue")
    playwright_api.expect(chips).to_contain_text("検索「CSV」")
    playwright_api.expect(page.locator(".issue")).to_have_count(1)

    # Ctrl+F で開く。Esc は、検索語があれば消し、空なら閉じる
    page.keyboard.press("Control+f")
    playwright_api.expect(page.locator(".search-pop input")).to_have_value("CSV")
    page.keyboard.press("Escape")
    playwright_api.expect(page.locator(".search-pop input")).to_have_value("")
    page.keyboard.press("Escape")
    playwright_api.expect(page.locator(".search-pop")).to_have_count(0)
    playwright_api.expect(chips).to_be_hidden()
    playwright_api.expect(page.locator(".issue")).to_have_count(2)  # クローズ済み(12)は、既定の「未完了」で出ない

    # チップの✕で消せる
    page.get_by_role("button", name="検索 (Ctrl+F)").click()
    page.locator(".search-pop input").fill("メール")
    page.keyboard.press("Enter")
    playwright_api.expect(chips).to_contain_text("メール")
    page.get_by_role("button", name="検索の条件を外す").click()
    playwright_api.expect(chips).to_be_hidden()
    playwright_api.expect(page.locator(".issue")).to_have_count(2)
    assert not page.errors, page.errors


def test_filter_popup_status_and_chips(page, remi, fake):
    """ステータスは「未完了(既定)/クローズ済み/すべて」と個別の選択。条件はチップに出て、✕で消せる。"""
    _open_list(page, remi, fake)
    button = page.get_by_role("button", name="絞り込み", exact=True)
    chips = page.locator(".cchips")
    playwright_api.expect(page.locator(".issue")).to_have_count(2)  # 既定は未完了
    button.click()
    pop = page.locator(".fpop")
    playwright_api.expect(pop.get_by_role("button", name=re.compile("^未完了"))).to_have_attribute("aria-pressed", "true")

    pop.get_by_role("button", name="クローズ済み").click()
    playwright_api.expect(page.locator(".issue")).to_have_count(1)
    playwright_api.expect(page.locator(".issue")).to_contain_text("終了済みの件")
    playwright_api.expect(chips).to_contain_text("ステータス: クローズ済み")
    pop.get_by_role("button", name="すべて", exact=True).click()
    playwright_api.expect(page.locator(".issue")).to_have_count(3)

    # 個別に選ぶと、3つは選択なしになる(複数選べる)
    pop.get_by_role("menuitemcheckbox", name=re.compile("^新規")).click()
    playwright_api.expect(pop.get_by_role("button", name="すべて", exact=True)).to_have_attribute("aria-pressed", "false")
    playwright_api.expect(chips).to_contain_text("ステータス: 新規")
    playwright_api.expect(page.locator(".issue")).to_have_count(2)
    assert remi.ctx.settings.get("list_status") == {"mode": "open", "names": ["新規"]}
    page.keyboard.press("Escape")

    # チップの✕で、既定の「未完了」に戻る
    page.get_by_role("button", name="ステータスの条件を外す").click()
    playwright_api.expect(chips).to_be_hidden()
    assert remi.ctx.settings.get("list_status") == {"mode": "open", "names": []}
    assert not page.errors, page.errors


def test_filter_attributes_and_clear_all(page, remi, fake):
    """担当者などの属性の絞り込み(項目ごとに開閉、チェック付きのリスト)と、「すべて解除」。"""
    _open_list(page, remi, fake)
    page.get_by_role("button", name="絞り込み", exact=True).click()
    pop = page.locator(".fpop")
    pop.get_by_role("button", name=re.compile("^担当者")).click()
    # 「自分」は、担当者のリストの先頭。件数が付く
    first = pop.locator(".plist .fit").first
    playwright_api.expect(first).to_contain_text("自分")
    first.click()
    playwright_api.expect(page.locator(".issue")).to_have_count(1)
    playwright_api.expect(page.locator(".cchip-filter")).to_contain_text("担当者: 自分")
    playwright_api.expect(page.get_by_role("button", name="絞り込み", exact=True)).to_have_attribute("aria-pressed", "true")
    page.keyboard.press("Escape")

    # 関係ボタン・検索も合わせて0件にして、「すべて解除」で既定に戻す
    page.get_by_role("button", name="担当", exact=True).click()
    page.get_by_role("button", name="検索 (Ctrl+F)").click()
    page.locator(".search-pop input").fill("メール")  # 件名にメールを含むのは、担当ではないチケット(11)だけ
    page.keyboard.press("Enter")
    playwright_api.expect(page.locator(".issue")).to_have_count(0)
    playwright_api.expect(page.locator(".empty")).to_contain_text("条件に一致するチケットはありません。")
    page.locator(".empty").get_by_role("button", name="すべて解除").click()
    playwright_api.expect(page.locator(".issue")).to_have_count(2)
    playwright_api.expect(page.locator(".cchips")).to_be_hidden()
    assert remi.ctx.settings.get("relation_filter") == "all"
    assert remi.ctx.settings.get("list_filter") == {"trackers": [], "priorities": [], "assignees": [], "due": []}
    assert not page.errors, page.errors


def test_sort_menu_tags_and_chip(page, remi, fake):
    """並べ替えのメニュー。既定以外は、チップと、行のタグ(優先度・期日など)で分かる。"""
    seed(fake)
    fake.add_issue(13, 2, "優先度が高い", priority_id=3, due_date="2020-01-01")
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.wait_for_selector(".issue-list li.issue")
    sort = page.get_by_role("button", name="並べ替え", exact=True)
    sort.click()
    page.get_by_role("menuitemcheckbox", name="優先度").click()
    # 優先度は高い順(降順)が初期値。「高め」が先頭
    playwright_api.expect(page.locator(".issue").first).to_contain_text("優先度が高い")
    playwright_api.expect(page.locator(".issue").first.locator(".skey")).to_have_text("優先度 高め")
    playwright_api.expect(page.locator(".cchip-sort")).to_contain_text("並べ替え: 優先度 ↓")
    assert remi.ctx.settings.get("list_sort") == {"key": "priority", "desc": True}

    # 期日: 近い順(昇順)。期限切れは、警告のタグ
    page.locator(".lbtn-sort").click()
    page.get_by_role("menuitemcheckbox", name="期日").click()
    playwright_api.expect(page.locator(".issue").first).to_contain_text("優先度が高い")
    playwright_api.expect(page.locator(".issue").first.locator(".skey.warn")).to_contain_text("期日 2020/1/1")
    page.locator(".lbtn-sort").click()
    page.get_by_role("button", name="降順 ↓").click()
    playwright_api.expect(page.locator(".cchip-sort")).to_contain_text("並べ替え: 期日 ↓")
    # 期日のないものは、降順でも最後(期日のあるチケットが先頭)
    playwright_api.expect(page.locator(".issue").first).to_contain_text("優先度が高い")
    playwright_api.expect(page.locator(".issue").last).not_to_contain_text("優先度が高い")

    # ✕で既定の並びに戻る
    page.get_by_role("button", name="並べ替えの条件を外す").click()
    playwright_api.expect(page.locator(".cchips")).to_be_hidden()
    playwright_api.expect(page.locator(".skey")).to_have_count(0)
    assert remi.ctx.settings.get("list_sort") == {"key": "updated", "desc": True}
    assert not page.errors, page.errors


def test_unread_first_is_in_settings_dialog(page, remi, fake):
    """「未読を上に」は、一覧のヘッダではなく、全体の設定の画面にある。"""
    _open_list(page, remi, fake, closed=False)
    assert page.get_by_label("未読を上に").count() == 0
    page.get_by_role("button", name="設定", exact=True).click()
    box = page.get_by_label("未読のチケットを上に表示する")
    playwright_api.expect(box).to_be_checked()
    box.uncheck()
    page.wait_for_timeout(300)
    assert remi.ctx.settings.get("unread_first") is False
    assert not page.errors, page.errors


def test_compact_density(page, remi, fake):
    """一覧の表示密度を切り替え、選んだ表示を次回も使う。コンパクトでは関係のラベルを1行目に出す。"""
    seed(fake)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.wait_for_selector(".issue-list li.issue")
    button = page.get_by_role("button", name="コンパクト表示")
    playwright_api.expect(button).to_have_attribute("aria-pressed", "false")
    assert page.locator(".issue .line3").count() == 2  # 標準: 3行目に関係のラベルと最新コメント

    button.click()
    playwright_api.expect(button).to_have_attribute("aria-pressed", "true")
    playwright_api.expect(page.locator(".issue-list.compact")).to_have_count(1)
    assert page.locator(".issue .line3").count() == 0
    row = page.locator(".issue", has_text="メールの宛先を追加したい")
    playwright_api.expect(row.locator(".line1 .tag").first).to_be_visible()
    assert remi.ctx.settings.get("list_density") == "compact"

    page.reload()  # 次に開いたときも、コンパクトのまま
    page.wait_for_selector(".issue-list.compact li.issue")
    page.get_by_role("button", name="コンパクト表示").click()
    playwright_api.expect(page.locator(".issue-list.compact")).to_have_count(0)
    assert remi.ctx.settings.get("list_density") == "standard"
    assert not page.errors, page.errors


def test_sort_applies_while_an_issue_is_open(page, remi, fake):
    """チケットを開いている間も、並べ替えは効く(「未読を上に」の並びの固定で、新しい並びを打ち消さない)。"""
    _open_list(page, remi, fake, closed=False)
    ids = "() => [...document.querySelectorAll('.issue')].map(e => Number(e.dataset.issue))"
    page.locator(".issue", has_text="#10").click()
    playwright_api.expect(page.locator(".pane-conv h1")).to_be_visible()
    page.get_by_role("button", name="並べ替え", exact=True).click()
    page.get_by_role("menuitemcheckbox", name="チケット番号").click()
    # 保存が終わって、一覧が変わってから、次のメニューを開く(メニューは、開いたときの設定で作る)
    playwright_api.expect(page.locator(".cchip-sort")).to_contain_text("並べ替え: チケット番号 ↑")
    page.wait_for_function(f"() => JSON.stringify(({ids})()) === '[10,11]'")  # チケットを開いていても、すぐ新しい並び
    page.locator(".lbtn-sort").click()
    page.get_by_role("button", name="降順 ↓").click()
    playwright_api.expect(page.locator(".cchip-sort")).to_contain_text("並べ替え: チケット番号 ↓")
    page.wait_for_function(f"() => JSON.stringify(({ids})()) === '[11,10]'")
    assert not page.errors, page.errors


def test_sort_button_closes_its_open_menu(page, remi, fake):
    """並べ替えのボタンを、メニューが開いたまま押すと、閉じる(検索・絞り込みと同じ。開き直さない)。"""
    _open_list(page, remi, fake, closed=False)
    button = page.locator(".lbtn-sort")
    button.click()
    playwright_api.expect(page.locator(".menu")).to_have_count(1)
    button.click()
    page.wait_for_timeout(200)
    playwright_api.expect(page.locator(".menu")).to_have_count(0)
    button.click()  # もう一度押せば、また開く
    playwright_api.expect(page.locator(".menu")).to_have_count(1)
    assert not page.errors, page.errors


def test_assignee_search_survives_ime_and_refresh(page, remi, fake):
    """担当者の名前検索は、日本語の変換中も、一覧の更新やキーボード操作でも、入力欄が作り直されず、フォーカスを失わない。"""
    _open_list(page, remi, fake)
    page.get_by_role("button", name="絞り込み", exact=True).click()
    pop = page.locator(".fpop")
    pop.get_by_role("button", name=re.compile("^担当者")).click()
    box = pop.locator(".find input")
    box.click()
    box.evaluate("e => { e.dataset.mark = 'same'; }")  # 作り直されたら、この印が消える
    cdp = page.context.new_cdp_session(page)
    cdp.send("Input.imeSetComposition", {"text": "さ", "selectionStart": 1, "selectionEnd": 1})
    cdp.send("Input.imeSetComposition", {"text": "さとう", "selectionStart": 3, "selectionEnd": 3})
    cdp.send("Input.insertText", {"text": "佐藤"})
    page.wait_for_timeout(200)
    assert box.input_value() == "佐藤"
    page.evaluate("() => window.remi.list.refresh()")  # 同期のイベントなどで、一覧が更新されても
    page.wait_for_timeout(400)
    playwright_api.expect(pop.locator(".find input")).to_be_focused()
    assert pop.locator(".find input").get_attribute("data-mark") == "same"

    # 閉じて開き直すと、前の検索語は残らない
    page.keyboard.press("Escape")
    page.get_by_role("button", name="絞り込み", exact=True).click()  # 開いていた項目(担当者)は、そのまま開く
    assert pop.locator(".find input").input_value() == ""
    assert not page.errors, page.errors


def test_filter_popup_keeps_keyboard_focus_after_toggle(page, remi, fake):
    """キーボード(Space)で選択肢を切り替えても、同じ項目にフォーカスが残る。"""
    _open_list(page, remi, fake)
    page.get_by_role("button", name="絞り込み", exact=True).click()
    item = page.locator(".fpop .fit", has_text="新規")
    item.focus()
    page.keyboard.press("Space")
    playwright_api.expect(page.locator(".cchip-filter")).to_contain_text("ステータス: 新規")
    focused = page.evaluate("() => document.activeElement.dataset.group + ':' + document.activeElement.dataset.value")
    assert focused == "status:新規"
    assert not page.errors, page.errors


def test_closed_only_scope_offers_all_statuses(page, remi, fake):
    """既定の「未完了」では何も出ないとき、「すべて解除」ではなく、ステータスを「すべて」にするボタンを出す。"""
    fake.add_project(1, "基幹システム")
    fake.add_project(2, "受注管理", parent=1)
    fake.add_issue(12, 2, "終了済みの件", status_id=5, watchers={ME})
    from remi.store.servers import Server

    remi.ctx.servers.save(Server(name="社内Redmine", url=fake.base_url), api_key=API_KEY)
    remi.sync.restart()
    page.goto(remi.server.start_url)
    page.wait_for_selector(".list-header")
    empty = page.locator(".empty")
    playwright_api.expect(empty).to_contain_text("条件に一致するチケットはありません。")
    empty.get_by_role("button", name="ステータスを「すべて」にする").click()
    playwright_api.expect(page.locator(".issue")).to_have_count(1)
    assert remi.ctx.settings.get("list_status") == {"mode": "all", "names": []}
    assert not page.errors, page.errors


def test_narrow_list_pane_does_not_overflow(page, remi, fake):
    """一覧のペインを最小幅(240px)まで狭めても、ヘッダ・行・絞り込みのポップアップが、はみ出したり折り返したりしない。"""
    seed(fake)
    fake.add_issue(13, 2, "優先度が高い", priority_id=3, due_date="2020-01-01", watchers={ME})
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.wait_for_selector(".issue-list li.issue")
    page.evaluate("() => { document.querySelector('.pane-list').style.width = '240px'; }")
    playwright_api.expect(page.locator(".pane-list.narrow")).to_have_count(1)
    # 標準の幅では、狭い表示にしない
    page.evaluate("() => { document.querySelector('.pane-list').style.width = '360px'; }")
    playwright_api.expect(page.locator(".pane-list.narrow")).to_have_count(0)
    page.evaluate("() => { document.querySelector('.pane-list').style.width = '240px'; }")
    playwright_api.expect(page.locator(".pane-list.narrow")).to_have_count(1)  # 幅の変化の検知は、少し遅れる

    # ヘッダ: タイトルは1行で、件数は数字だけ。ボタンの隙間は詰まる
    title = page.locator(".list-title h2")
    assert title.evaluate("e => e.getBoundingClientRect().height") < 24
    assert page.locator(".list-title .count").evaluate("e => getComputedStyle(e, '::after').content") == '"3"'
    assert page.locator(".list-title").evaluate("e => e.scrollWidth <= e.clientWidth")

    # 行: コンパクト + 期日の並べ替えでも、一覧に横スクロールが出ない。ステータスのラベルは縦に折り返さない
    page.get_by_role("button", name="コンパクト表示").click()
    page.locator(".lbtn-sort").click()
    page.get_by_role("menuitemcheckbox", name="期日").click()
    playwright_api.expect(page.locator(".cchip-sort")).to_be_visible()
    page.wait_for_timeout(300)
    assert page.locator(".issue-list").evaluate("e => e.scrollWidth <= e.clientWidth")
    assert page.locator(".issue .pill").first.evaluate("e => e.getBoundingClientRect().height") < 24

    # ポップアップ: 大分類の3つのボタンが、枠の中に1行で収まる
    page.get_by_role("button", name="絞り込み", exact=True).click()
    box = page.locator(".fpop").bounding_box()
    buttons = page.locator(".fpop .modes .o")
    assert buttons.count() == 3
    tops = set()
    for i in range(3):
        b = buttons.nth(i).bounding_box()
        assert b["x"] >= box["x"] and b["x"] + b["width"] <= box["x"] + box["width"] + 1
        tops.add(round(b["y"]))
    assert len(tops) == 1
    assert not page.errors, page.errors


def test_server_dialog_finds_the_top_url_from_a_project_url(page, remi, fake):
    """プロジェクトのURLを入れても、Redmine のトップのアドレスを探して、URLの欄を直す。結果は「接続できました。」だけ。"""
    seed(fake)
    fake.prefix = "/redmine"
    page.goto(remi.server.start_url)
    page.get_by_role("button", name="Redmineサーバーを登録").click()
    dialog = page.get_by_role("dialog", name="サーバーの登録")
    dialog.get_by_label("URL", exact=True).fill(f"{fake.base_url}/redmine/projects/1/issues?set_filter=1")
    dialog.get_by_label("APIアクセスキー").fill(API_KEY)
    dialog.get_by_role("button", name="接続テスト").click()
    status = dialog.get_by_role("status")
    playwright_api.expect(status).to_have_text("接続できました。")
    playwright_api.expect(dialog.get_by_label("URL", exact=True)).to_have_value(f"{fake.base_url}/redmine")
    dialog.get_by_label("表示名").fill("社内Redmine")
    dialog.get_by_role("button", name="登録").click()  # 直したURLのまま、テスト済みとして登録できる
    playwright_api.expect(dialog).to_be_hidden()
    assert remi.ctx.servers.list()[0].url == f"{fake.base_url}/redmine"
    assert not page.errors, page.errors


def test_server_dialog_failure_shows_two_short_lines(page, remi, fake):
    """失敗したときは、「接続できませんでした。」と、確認すること(URLとREST APIの可能性)の2行だけ。管理者への依頼は出さない。"""
    seed(fake)
    fake.rest_enabled = False
    page.goto(remi.server.start_url)
    page.get_by_role("button", name="Redmineサーバーを登録").click()
    dialog = page.get_by_role("dialog", name="サーバーの登録")
    dialog.get_by_label("URL", exact=True).fill(f"{fake.base_url}/projects/1")
    dialog.get_by_label("APIアクセスキー").fill(API_KEY)
    dialog.get_by_role("button", name="接続テスト").click()
    status = dialog.get_by_role("status")
    playwright_api.expect(status).to_contain_text("接続できませんでした。")
    playwright_api.expect(status).to_contain_text("URLが正しいか確認してください。RedmineのREST APIが無効になっている可能性もあります。")
    assert "管理者" not in status.inner_text()
    playwright_api.expect(dialog.get_by_label("URL", exact=True)).to_have_value(f"{fake.base_url}/projects/1")  # 直さない
    playwright_api.expect(dialog.get_by_role("button", name="登録")).to_be_disabled()
    assert not page.errors, page.errors


# --- 全体の点検で見つかった不具合(0.23.0) ---------------------------------------------------
def test_header_follows_changes_made_by_others(page, remi, fake):
    """開いている間に、ほかの人がステータスを変えたら、ヘッダの選択欄も新しい値になる。"""
    _open_app(page, remi, fake)
    page.locator(".issue", has_text="CSVが文字化けする").click()
    status = page.get_by_label("ステータス", exact=True)
    playwright_api.expect(status).to_be_enabled()
    time.sleep(1.1)  # updated_on は秒単位
    fake.set_status(10, 2)  # ほかの人が「進行中」にした
    page.keyboard.press("F5")
    playwright_api.expect(page.locator(".timeline")).to_contain_text("進行中", timeout=15000)
    page.wait_for_function("""() => {
      const s = [...document.querySelectorAll('.conv-header select')].find((e) => e.getAttribute('aria-label') === 'ステータス');
      return s && !s.disabled && s.options[s.selectedIndex].text === '進行中';
    }""", timeout=15000)
    assert not page.errors, page.errors


def test_server_status_dot_is_shown(page, remi, fake):
    _open_app(page, remi, fake)
    dot = page.locator(".tree .node.server .dot")
    playwright_api.expect(dot).to_have_class("dot ok")
    playwright_api.expect(dot).to_have_attribute("title", "接続中")


def test_mark_unread_is_disabled_when_it_cannot_be_done(page, remi, fake):
    seed(fake)
    fake.add_issue(12, 2, "自分のメモだけ", watchers={ME})
    fake.add_journal(12, ME, "自分のメモ")
    add_server(remi, fake)
    end = time.monotonic() + 20
    while time.monotonic() < end and not remi.ctx.issues.journals_synced(1, 12):
        time.sleep(0.1)
    page.goto(remi.server.start_url)
    page.locator(".issue", has_text="自分のメモだけ").click(button="right")
    playwright_api.expect(page.get_by_role("menuitem", name="未読に戻す")).to_be_disabled()
    page.keyboard.press("Escape")
    page.locator(".issue", has_text="CSVが文字化けする").click(button="right")
    page.get_by_role("menuitem", name="未読に戻す").click()
    playwright_api.expect(page.locator(".issue.unread", has_text="CSVが文字化けする")).to_be_visible()


def test_tree_menu_opens_redmine_like_other_menus(page, remi, fake):
    _open_app(page, remi, fake)
    page.locator(".tree .node", has_text="受注管理").first.click(button="right")
    # メニューは、プロジェクトのURLを問い合わせてから開く
    playwright_api.expect(page.get_by_role("menuitem", name="Remiで開く")).to_have_count(1)
    playwright_api.expect(page.get_by_role("menuitem", name="外部ブラウザで開く")).to_have_count(1)


def test_changing_server_address_requires_the_key(page, remi, fake):
    _open_app(page, remi, fake)
    page.evaluate("async () => { const [s] = await (await fetch('/api/listServers', {method: 'POST', "
                  "headers: {'Content-Type': 'application/json'}, body: '{}'})).json().then((b) => b.result); "
                  "window.remi.openServerDialog(s); }")
    dialog = page.get_by_role("dialog", name="サーバーの編集")
    dialog.get_by_label("URL", exact=True).fill("https://other.example.com")
    playwright_api.expect(dialog.get_by_role("button", name="接続テスト")).to_be_disabled()
    playwright_api.expect(dialog).to_contain_text("APIアクセスキーを入力してください")
    dialog.get_by_label("APIアクセスキー").fill("new-key")
    playwright_api.expect(dialog.get_by_role("button", name="接続テスト")).to_be_enabled()
    dialog.get_by_label("APIアクセスキー").fill("")
    dialog.get_by_label("URL", exact=True).fill(fake.base_url)  # 同じ接続先なら、今のキーのまま
    playwright_api.expect(dialog.get_by_role("button", name="接続テスト")).to_be_enabled()


def test_markdown_image_with_japanese_name_is_shown(page, remi, fake):
    seed(fake)
    fake.add_attachment(10, 78, "スクリーンショット 1.png", _png())
    fake.add_journal(10, 7, "画面です\n\n![](<スクリーンショット 1.png>)")
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    page.locator(".issue", has_text="CSVが文字化けする").click()
    playwright_api.expect(page.locator('.timeline img[src$="/78"]')).to_have_count(1, timeout=15000)
    assert page.locator(".timeline .missing-image").count() == 0


def test_condition_chips_are_distinguishable_in_dark_theme(page, remi, fake):
    """ダークでは、条件のチップに色の枠線を付け、背景の色も濃くする(16%では、どれも同じ暗い灰色に見える)。"""
    remi.ctx.settings.set("theme", "dark")
    remi.ctx.settings.set("list_search", "CSV")
    remi.ctx.settings.set("list_sort", {"key": "priority", "desc": True})
    _open_app(page, remi, fake)
    page.wait_for_selector(".cchip-sort")
    styles = page.evaluate("""() => ['.cchip-search', '.cchip-sort'].map((s) => {
      const st = getComputedStyle(document.querySelector(s));
      return [st.backgroundColor, st.boxShadow];
    })""")
    (search_bg, search_edge), (sort_bg, sort_edge) = styles
    assert search_bg != sort_bg
    assert "inset" in search_edge and "inset" in sort_edge
    # 選択中の行の左の線・状態の丸の色(両テーマで同じ)
    page.locator(".issue-list li.issue").first.click()
    shadow = page.locator(".issue.selected").evaluate("e => getComputedStyle(e).boxShadow")
    assert "inset" in shadow
    dot = page.locator(".tree .node.server .dot").evaluate("e => getComputedStyle(e).backgroundColor")
    assert dot == "rgb(45, 164, 78)"
    assert not page.errors, page.errors


# --- サーバーの並べ替え ---------------------------------------------------------------------
def _three_servers(page, remi, fake):
    from remi.store.servers import Server

    seed(fake)
    ids = [remi.ctx.servers.save(Server(name=n, url=fake.base_url, sort_order=i), api_key=API_KEY).server_id
           for i, n in enumerate(("Aサーバー", "Bサーバー", "Cサーバー"))]
    remi.sync.restart()
    end = time.monotonic() + 30
    while time.monotonic() < end and not all(remi.ctx.issues.journals_synced(s, 10) for s in ids):
        time.sleep(0.1)
    page.goto(remi.server.start_url)
    page.wait_for_selector(".tree .node.server")
    page.wait_for_function("() => document.querySelectorAll('.tree > li[data-server-id] > ul[role=group]').length === 3")
    return ids


def _tree_order(page):
    return page.locator(".tree > li[data-server-id] > .node.server .name").all_inner_texts()


def test_reorder_servers_by_dragging_in_the_tree(page, remi, fake):
    """サーバーの行をドラッグして並べ替える。ドラッグ中はすべて折りたたみ、終わったら元の開閉に戻る。"""
    a, b, c = _three_servers(page, remi, fake)
    # Bサーバーは、あらかじめ折りたたんでおく(終わったあとも折りたたんだまま)
    page.locator(".tree > li[data-server-id] > .node.server", has_text="Bサーバー").locator(".twisty").click()
    page.wait_for_function("() => document.querySelectorAll('.tree > li[data-server-id] > ul[role=group]').length === 2")
    src = page.locator(".tree > li[data-server-id] > .node.server", has_text="Cサーバー").bounding_box()
    page.mouse.move(src["x"] + 60, src["y"] + src["height"] / 2)
    page.mouse.down()
    page.mouse.move(src["x"] + 60, src["y"] - 20, steps=3)  # 動かし始めると、すべて折りたたむ
    page.wait_for_function("() => !document.querySelector('.tree > li[data-server-id] > ul[role=group]')")
    top = page.locator(".tree > li[data-server-id] > .node.server", has_text="Aサーバー").bounding_box()
    page.mouse.move(top["x"] + 60, top["y"] + 3, steps=5)  # Aサーバーの上半分: その前に入れる
    playwright_api.expect(page.locator(".tree .drop-line")).to_have_count(1)
    playwright_api.expect(page.locator(".drag-ghost")).to_contain_text("Cサーバー")
    page.mouse.up()
    page.wait_for_function("() => document.querySelectorAll('.tree > li[data-server-id] > ul[role=group]').length === 2")
    assert _tree_order(page) == ["Cサーバー", "Aサーバー", "Bサーバー"]
    playwright_api.expect(page.locator(".tree .drop-line")).to_have_count(0)
    end = time.monotonic() + 10
    while time.monotonic() < end and [s.server_id for s in remi.ctx.servers.list()] != [c, a, b]:
        time.sleep(0.1)
    assert [s.server_id for s in remi.ctx.servers.list()] == [c, a, b]
    # 開いていたものは開いたまま、折りたたんでいたBサーバーは折りたたんだまま
    assert page.locator(".tree > li[data-server-id]", has_text="Bサーバー").locator(":scope > ul[role=group]").count() == 0
    assert page.locator(".tree > li[data-server-id]", has_text="Aサーバー").locator(":scope > ul[role=group]").count() == 1
    assert not page.errors, page.errors


def test_reordering_in_the_tree_can_be_cancelled_with_escape(page, remi, fake):
    a, b, c = _three_servers(page, remi, fake)
    src = page.locator(".tree > li[data-server-id] > .node.server", has_text="Aサーバー").bounding_box()
    page.mouse.move(src["x"] + 60, src["y"] + src["height"] / 2)
    page.mouse.down()
    page.mouse.move(src["x"] + 60, src["y"] + 200, steps=5)
    playwright_api.expect(page.locator(".tree .drop-line")).to_have_count(1)
    page.keyboard.press("Escape")
    page.mouse.up()
    page.wait_for_function("() => document.querySelectorAll('.tree > li[data-server-id] > ul[role=group]').length === 3")
    assert _tree_order(page) == ["Aサーバー", "Bサーバー", "Cサーバー"]
    assert [s.server_id for s in remi.ctx.servers.list()] == [a, b, c]
    # クリックはふつうに選択になる(ドラッグとみなさない)
    page.locator(".tree > li[data-server-id] > .node.server", has_text="Bサーバー").locator(".name").click()
    playwright_api.expect(page.locator(".list-title h2")).to_have_text("Bサーバー")


def test_reorder_servers_in_the_settings_list(page, remi, fake):
    a, b, c = _three_servers(page, remi, fake)
    page.get_by_role("button", name="設定").first.click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("tab", name="サーバー").click()
    rows = dialog.locator(".server-list li .info > div:first-child")
    playwright_api.expect(rows).to_have_text(["Aサーバー", "Bサーバー", "Cサーバー"])
    # キーボード: つまみを選んで Alt+↓
    grip = dialog.get_by_role("button", name="Aサーバー の並べ替え(Alt+↑/↓)")
    grip.focus()
    page.keyboard.press("Alt+ArrowDown")
    playwright_api.expect(rows).to_have_text(["Bサーバー", "Aサーバー", "Cサーバー"])
    playwright_api.expect(dialog.get_by_role("button", name="Aサーバー の並べ替え(Alt+↑/↓)")).to_be_focused()
    # ドラッグ: Cサーバーを先頭へ
    src = dialog.get_by_role("button", name="Cサーバー の並べ替え(Alt+↑/↓)").bounding_box()
    first = dialog.locator(".server-list li").first.bounding_box()
    page.mouse.move(src["x"] + src["width"] / 2, src["y"] + src["height"] / 2)
    page.mouse.down()
    page.mouse.move(src["x"] + 10, first["y"] + 4, steps=6)
    playwright_api.expect(dialog.locator(".drop-line")).to_have_count(1)
    page.mouse.up()
    playwright_api.expect(rows).to_have_text(["Cサーバー", "Bサーバー", "Aサーバー"])
    playwright_api.expect(dialog).to_be_visible()  # ドラッグでダイアログは閉じない
    end = time.monotonic() + 10
    while time.monotonic() < end and [s.server_id for s in remi.ctx.servers.list()] != [c, b, a]:
        time.sleep(0.1)
    assert [s.server_id for s in remi.ctx.servers.list()] == [c, b, a]
    # ツリーにも反映する
    page.wait_for_function("() => [...document.querySelectorAll('.tree > li[data-server-id] > .node.server .name')]"
                           ".map((e) => e.textContent).join() === 'Cサーバー,Bサーバー,Aサーバー'")
    assert not page.errors, page.errors


def test_no_reorder_handle_with_a_single_server(page, remi, fake):
    _open_app(page, remi, fake)
    assert page.locator(".tree .node.server.draggable").count() == 0
    page.get_by_role("button", name="設定").first.click()
    page.get_by_role("dialog").get_by_role("tab", name="サーバー").click()
    page.wait_for_selector(".server-list li .info")
    assert page.locator(".server-list .grip").count() == 0


# --- 更新のお知らせ・一覧の上部 ------------------------------------------------------------------
class _Checker:
    folder = None

    def __init__(self, result):
        self.result = result

    def check(self):
        return self.result


def test_update_bar(page, remi, fake, tmp_path, monkeypatch):
    """更新があれば、上部に帯を出す。「あとで」で消し、「更新して再起動」は確認してから Remi-update.bat を起動する。"""
    from remi.system import update_check
    from remi.system.update_check import UpdateWatcher

    batch = tmp_path / "Remi-update.bat"
    batch.write_text("@echo off\n")
    opened = []
    monkeypatch.setattr(update_check, "open_batch", opened.append)
    checker = _Checker(False)
    remi.api.updates = UpdateWatcher(remi.ctx.bus, checker, batch)
    _open_app(page, remi, fake)
    bar = page.locator(".update-bar")
    playwright_api.expect(bar).to_be_hidden()
    # 6時間ごとの確認で見つかった: 開いている画面にも出る
    checker.result = True
    remi.api.updates.check_now()
    playwright_api.expect(bar).to_be_visible()
    playwright_api.expect(bar).to_contain_text("Remi の更新があります")
    bar.get_by_role("button", name="更新して再起動").click()
    dialog = page.get_by_role("dialog", name="更新して再起動")
    dialog.get_by_role("button", name="キャンセル").click()
    assert opened == []
    bar.get_by_role("button", name="更新して再起動").click()
    dialog.get_by_role("button", name="更新して再起動").click()
    page.wait_for_selector(".toast:has-text('更新を始めました')")
    assert opened == [batch]
    bar.get_by_role("button", name="あとで").click()
    playwright_api.expect(bar).to_be_hidden()
    page.reload()  # 次の起動までは出さない
    page.wait_for_selector(".issue-list li.issue")
    playwright_api.expect(bar).to_be_hidden()
    assert not page.errors, page.errors


def test_update_bar_without_the_batch(page, remi, fake, tmp_path):
    from remi.system.update_check import UpdateWatcher

    remi.api.updates = UpdateWatcher(remi.ctx.bus, _Checker(True), tmp_path / "Remi-update.bat")
    remi.api.updates.check_now()
    _open_app(page, remi, fake)
    bar = page.locator(".update-bar")
    playwright_api.expect(bar).to_contain_text("Remi-update.bat が見つからない")
    playwright_api.expect(bar.get_by_role("button", name="更新して再起動")).to_be_disabled()


def test_list_header_shows_the_location_on_its_own_line(page, remi, fake):
    """一覧の上部: 1行目に場所、2行目に名前・ボタン・件数。場所がなければ1行目は出さない。"""
    _open_app(page, remi, fake)
    row = page.locator(".list-header .crumb-row")
    page.locator(".tree .node", has_text="受注管理").first.click()
    playwright_api.expect(row).to_have_text("基幹システム")
    playwright_api.expect(page.locator(".list-title h2")).to_have_text("受注管理")
    playwright_api.expect(page.locator(".list-title .count")).to_have_text("2件")
    page.locator(".tree .node", has_text="基幹システム").first.click()
    playwright_api.expect(row).to_have_text("社内Redmine")  # 最上位のプロジェクト: 場所はサーバー
    page.locator(".tree .node", has_text="すべて").first.click()
    playwright_api.expect(row).to_be_hidden()  # サーバー全体: 場所はない(1行で出す)
    playwright_api.expect(page.locator(".list-title h2")).to_have_text("社内Redmine")


def test_open_issue_from_notification_updates_tree_and_list_selection(page, remi, fake):
    """通知から開いたとき、左(ツリー)・真ん中(一覧)の選択も、そのチケットに合わせる"""
    seed(fake)
    fake.add_project(3, "社内ポータル")
    fake.add_issue(30, 3, "ポータルの要望", watchers={ME})
    add_server(remi, fake)
    end = time.monotonic() + 20
    while time.monotonic() < end and remi.ctx.issues.get(1, 30) is None:
        time.sleep(0.1)
    page.goto(remi.server.start_url)
    tree = page.locator(".tree")
    selected = tree.locator(".node.selected")
    conv = page.locator(".pane-conv")
    # 別のプロジェクトを選んでいる → チケットのプロジェクトに切り替わり、一覧にそのチケットが強調される
    tree.locator(".node", has_text="社内ポータル").click()
    playwright_api.expect(page.locator(".list-title h2")).to_have_text("社内ポータル")
    remi.ctx.bus.publish("openIssue", serverId=1, issueId=10)
    playwright_api.expect(conv.locator("h1")).to_contain_text("CSVが文字化けする", timeout=15000)
    playwright_api.expect(selected).to_have_text(re.compile("受注管理"))
    playwright_api.expect(page.locator(".list-title h2")).to_have_text("受注管理")
    playwright_api.expect(page.locator(".issue.selected")).to_contain_text("CSVが文字化けする")
    # そのチケットが今の一覧にあるときは、選択を変えない(サーバー全体のまま)
    tree.locator(".node", has_text="社内Redmine").first.click()
    playwright_api.expect(page.locator(".list-title h2")).to_have_text("社内Redmine")
    remi.ctx.bus.publish("openIssue", serverId=1, issueId=30)
    playwright_api.expect(conv.locator("h1")).to_contain_text("ポータルの要望", timeout=15000)
    playwright_api.expect(page.locator(".list-title h2")).to_have_text("社内Redmine")
    playwright_api.expect(page.locator(".issue.selected")).to_contain_text("ポータルの要望")
    assert not page.errors, page.errors


def test_selection_back_and_forward(page, remi, fake):
    """ツリーの選択と開いたチケットを、戻る・進む(ボタン・Alt+←/→・マウスのボタン)でたどれる"""
    seed(fake)
    fake.add_project(3, "社内ポータル")
    fake.add_issue(30, 3, "ポータルの要望", watchers={ME})
    add_server(remi, fake)
    end = time.monotonic() + 20
    while time.monotonic() < end and remi.ctx.issues.get(1, 30) is None:
        time.sleep(0.1)
    page.goto(remi.server.start_url)
    header = page.locator(".tree-header")
    back = header.get_by_role("button", name=re.compile("^前の選択に戻る"))
    forward = header.get_by_role("button", name=re.compile("^次の選択に進む"))
    title = page.locator(".list-title h2")
    conv = page.locator(".pane-conv")
    # 文字は出さない(アイコンだけ。説明は aria-label とツールチップ)
    playwright_api.expect(header.locator(".label")).to_have_count(0)
    playwright_api.expect(back).to_be_disabled()
    playwright_api.expect(forward).to_be_disabled()

    page.locator(".tree .node", has_text="社内ポータル").click()
    playwright_api.expect(title).to_have_text("社内ポータル")
    page.locator(".tree .node", has_text="受注管理").click()
    playwright_api.expect(title).to_have_text("受注管理")
    page.locator(".issue", has_text="CSVが文字化けする").click()
    playwright_api.expect(conv.locator("h1")).to_contain_text("CSVが文字化けする", timeout=15000)
    playwright_api.expect(back).to_be_enabled()
    playwright_api.expect(forward).to_be_disabled()

    # 戻る(ボタン): チケットを開く前の状態 → 左・真ん中・右が前の状態に揃う
    back.click()
    playwright_api.expect(conv.locator("h1")).to_have_count(0)
    playwright_api.expect(title).to_have_text("受注管理")
    # Alt+←: 前のプロジェクト
    page.locator(".tree").focus()
    page.keyboard.press("Alt+ArrowLeft")
    playwright_api.expect(title).to_have_text("社内ポータル")
    playwright_api.expect(page.locator(".tree .node.selected")).to_contain_text("社内ポータル")
    # マウスの「進む」ボタン(ボタン4)
    page.evaluate("dispatchEvent(new MouseEvent('mouseup', {button: 4, cancelable: true}))")
    playwright_api.expect(title).to_have_text("受注管理")
    # 進む(ボタン)でチケットを開いた状態に戻る。一覧でも強調される
    forward.click()
    playwright_api.expect(conv.locator("h1")).to_contain_text("CSVが文字化けする", timeout=15000)
    playwright_api.expect(page.locator(".issue.selected")).to_contain_text("CSVが文字化けする")
    playwright_api.expect(forward).to_be_disabled()
    # マウスの「戻る」ボタン(ボタン3)
    page.evaluate("dispatchEvent(new MouseEvent('mouseup', {button: 3, cancelable: true}))")
    playwright_api.expect(conv.locator("h1")).to_have_count(0)
    playwright_api.expect(forward).to_be_enabled()  # 戻る処理が終わった印(処理中の操作は履歴に積まない)

    # 戻ったあとに新しく選ぶと、進む側の履歴は捨てる
    page.locator(".tree .node", has_text="社内ポータル").click()
    playwright_api.expect(title).to_have_text("社内ポータル")
    playwright_api.expect(forward).to_be_disabled()

    # 入力欄の中の Alt+← は、履歴を戻さない
    page.locator(".issue", has_text="ポータルの要望").click()
    box = conv.get_by_role("textbox", name="コメント")
    playwright_api.expect(box).to_be_visible(timeout=15000)
    box.focus()
    page.keyboard.press("Alt+ArrowLeft")
    playwright_api.expect(conv.locator("h1")).to_contain_text("ポータルの要望")
    assert not page.errors, page.errors


def test_version_event_cards_and_deleted_issue_link(page, remi, fake):
    """変化の記録はカード(チップ+時刻 / #番号+件名)で出す。削除されたチケットのリンクは、ページを開かずトーストで知らせる"""
    seed(fake)
    fake.add_version(5, 2, "v1.0", due_date="2026-12-01")
    long_subject = "とても長い件名" * 20
    fake.add_issue(20, 2, long_subject, assigned_to=7, version=5)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    playwright_api.expect(page.locator(".issue")).to_have_count(2, timeout=20000)
    row = page.locator(".tree .node", has_text="基幹システム").first
    row.hover()
    row.locator(".node-gear").click()
    page.get_by_role("group", name="マイルストーンを表示する").get_by_role("button", name="オン").click()
    version = page.locator(".tree .node.version", has_text="v1.0")
    playwright_api.expect(version).to_be_visible(timeout=20000)
    version.click()
    conv = page.locator(".pane-conv")
    playwright_api.expect(conv.locator("h1")).to_contain_text("v1.0")
    # バージョンから外れ、そのチケット自体が削除された(Remi のキャッシュからも消えた)
    fake.set_version(20, None)
    del fake.issues[20]
    remi.ctx.issues.delete_issues(1, [20])
    remi.sync.sync_now()
    card = conv.locator(".version-event.removed")
    playwright_api.expect(card).to_be_visible(timeout=20000)
    # カード: 左にチップと時刻、右に #番号 と件名(長くても、カードの幅からはみ出さない)
    playwright_api.expect(card.locator(".side .badge")).to_have_text("除外")
    playwright_api.expect(card.locator(".side .time")).to_have_text(re.compile(r"\d+:\d+"))
    playwright_api.expect(card.locator(".body .issue-ref")).to_have_text("#20")
    box, parent = card.bounding_box(), conv.locator(".version-timeline").bounding_box()
    assert box["x"] + box["width"] <= parent["x"] + parent["width"] + 1
    # 押すと、Redmine を開かずに、削除された旨をトーストで知らせる
    opened = []
    remi.api.openRedmine = lambda url, external=False: opened.append(url)  # noqa: ARG005
    card.locator(".issue-ref").click()
    playwright_api.expect(page.locator(".toast")).to_have_text("#20 は Redmine にありません(削除された可能性があります)")
    assert not opened
    assert not page.errors, page.errors


def test_version_events_filter_by_kind(page, remi, fake):
    """バージョンの変化の記録は、種類のボタン(1つだけ選ぶ)で絞り込める。選択は、別のバージョンを開くと「すべて」に戻る"""
    seed(fake)
    fake.add_version(5, 2, "v1.0", due_date="2026-12-01")
    fake.add_version(6, 2, "v2.0")
    fake.add_issue(20, 2, "最初のチケット", assigned_to=7, version=5)
    fake.add_issue(22, 2, "終わらせるチケット", assigned_to=7, version=5)
    add_server(remi, fake)
    page.goto(remi.server.start_url)
    playwright_api.expect(page.locator(".issue")).to_have_count(2, timeout=20000)
    row = page.locator(".tree .node", has_text="基幹システム").first
    row.hover()
    row.locator(".node-gear").click()
    page.get_by_role("group", name="マイルストーンを表示する").get_by_role("button", name="オン").click()
    version = page.locator(".tree .node.version", has_text="v1.0")
    playwright_api.expect(version).to_be_visible(timeout=20000)
    version.click()
    conv = page.locator(".pane-conv")
    playwright_api.expect(conv.locator("h1")).to_contain_text("v1.0")
    filt = conv.locator(".version-filter")
    playwright_api.expect(filt).to_have_count(0)  # 変化がないときは、絞り込みを出さない
    # 追加・完了・除外・期日の変更が届く
    fake.add_issue(21, 2, "追加されたチケット", assigned_to=7)
    fake.set_version(21, 5)
    fake.set_version(20, None)
    fake.set_status(22, 5)
    fake.update_version(5, due_date="2026-12-24")
    remi.sync.sync_now()
    cards = conv.locator(".version-event")
    playwright_api.expect(conv.locator(".version-event.removed")).to_be_visible(timeout=20000)
    labels = filt.locator("button")
    playwright_api.expect(labels).to_have_text(["すべて", "追加", "完了", "除外", "その他"])  # 履歴にある種類だけ。並びは決まっている
    playwright_api.expect(filt.get_by_role("button", name="すべて")).to_have_attribute("aria-pressed", "true")
    total = cards.count()
    assert total >= 4
    # 1つだけ選ぶ: 選んだ種類のカードだけが残る
    filt.get_by_role("button", name="除外").click()
    playwright_api.expect(cards).to_have_count(1)
    playwright_api.expect(cards.first).to_contain_text("#20")
    playwright_api.expect(filt.get_by_role("button", name="除外")).to_have_attribute("aria-pressed", "true")
    filt.get_by_role("button", name="追加").click()  # ほかを選ぶと、前の選択は外れる
    playwright_api.expect(cards).to_have_count(1)
    playwright_api.expect(cards.first).to_contain_text("#21")
    playwright_api.expect(filt.get_by_role("button", name="除外")).to_have_attribute("aria-pressed", "false")
    filt.get_by_role("button", name="その他").click()
    playwright_api.expect(cards.first).to_contain_text("期日")
    # 絞り込んだまま新しい変化が届いても、選択は保たれる
    filt.get_by_role("button", name="追加").click()
    fake.add_issue(23, 2, "もう1つ追加", assigned_to=7)
    fake.set_version(23, 5)
    remi.sync.sync_now()
    playwright_api.expect(cards).to_have_count(2, timeout=20000)
    playwright_api.expect(filt.get_by_role("button", name="追加")).to_have_attribute("aria-pressed", "true")
    # 別のバージョンを開いて戻ると、「すべて」に戻る
    page.locator(".tree .node.version", has_text="v2.0").click()
    playwright_api.expect(conv.locator("h1")).to_contain_text("v2.0")
    version.click()
    playwright_api.expect(conv.locator("h1")).to_contain_text("v1.0")
    playwright_api.expect(filt.get_by_role("button", name="すべて")).to_have_attribute("aria-pressed", "true")
    playwright_api.expect(cards).to_have_count(total + 1)
    assert not page.errors, page.errors
