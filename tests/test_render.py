import pytest

from remi.render.details import DetailNames, describe_detail
from remi.render.markup import RenderContext, render


def ctx(fmt="common_mark", **kw):
    return RenderContext(1, "https://rm.example", fmt, {"shot.png": 5, "log.txt": 6}, my_login="okamo",
                         project_identifier="order", **kw)


def test_markdown_basics_and_code():
    out = render("**太字** と `#12`\n\n```python\nx = '<b>'\n```", ctx())
    assert "<strong>太字</strong>" in out
    assert "<code>#12</code>" in out  # インラインコード内は番号リンクにしない
    assert 'class="code hl"' in out and "&lt;b&gt;" in out


def test_hardbreaks_like_redmine():
    assert "<br>" in render("1行目\n2行目", ctx())


def test_redmine_refs():
    out = render("#1198 と #1234#note-3、#note-2、@okamo と @tanaka、r55、[[Wiki Page]]", ctx())
    assert 'href="remi://issue/1198"' in out
    assert 'href="remi://issue/1234#note-3"' in out
    assert 'href="remi://note/2"' in out
    assert 'class="mention me">@okamo' in out and 'class="mention">@tanaka' in out
    assert "repository/revisions/55" in out
    assert "projects/order/wiki/Wiki_Page" in out


def test_escaped_refs_and_urls_are_not_linked():
    out = render("!#123 と https://x.example/page#45 と a@b.example", ctx())
    assert "remi://issue/123" not in out and "remi://issue/45" not in out
    assert "mention" not in out


def test_images_are_resolved_to_attachments():
    out = render("![画面](shot.png) ![外部](https://evil.example/a.png) ![無い](missing.png)", ctx())
    assert '<img src="/attachments/1/5"' in out
    assert "evil.example/a.png" in out and "<img" in out.split("evil")[0]
    assert out.count("<img") == 1  # 外部画像と存在しない添付は読み込まない
    assert "[無い]" in out
    out = render("![外部](https://x.example/a.png)", ctx(allow_external_images=True))
    assert '/external-image?url=https%3A%2F%2Fx.example%2Fa.png' in out


@pytest.mark.parametrize("payload", [
    "<script>alert(1)</script>",
    '<img src=x onerror="alert(1)">',
    '<a href="javascript:alert(1)">x</a>',
    '<span style="position:fixed;color:red">x</span>',
    '<iframe src="https://evil.example"></iframe>',
])
@pytest.mark.parametrize("fmt", ["common_mark", "textile", "plain"])
def test_sanitized(payload, fmt):
    out = render(payload, ctx(fmt))
    # タグ・属性として残っていないこと(プレーンテキストでは文字としてエスケープされる)
    import re

    tags = re.findall(r"<[a-z][^>]*>", out.lower())
    assert not [x for x in tags if re.search(r"onerror|javascript:|position|<script|<iframe|src=x", x)], tags


def test_textile():
    out = render("h2. 見出し\n\n!shot.png!\n\n<pre><code class=\"ruby\">puts 1 < 2</code></pre>\n\n%{color:red}赤% @code #9@", ctx("textile"))
    assert "<h2>見出し</h2>" in out
    assert '<img alt="" src="/attachments/1/5">' in out or 'src="/attachments/1/5"' in out
    assert 'data-lang="ruby"' in out and "&lt;" in out
    assert 'style="color:red"' in out
    assert "<code>code #9</code>" in out


def test_task_list_and_tables():
    out = render("- [ ] todo\n- [x] done\n\n| a | b |\n|---|---|\n| 1 | 2 |", ctx())
    assert "☐ todo" in out and "☑ done" in out and "<table>" in out


def test_macros():
    out = render("{{collapse(詳細)\n中身 #5\n}}\n\n{{toc}}", ctx())
    assert "<details" in out and "<summary>詳細</summary>" in out and "remi://issue/5" in out
    assert '<span class="macro">{{toc}}</span>' in out


def test_attachment_ref():
    out = render("attachment:log.txt と attachment:shot.png", ctx())
    assert 'href="/attachments/1/6"' in out and "<img" in out


def test_plain_text():
    out = render("<b>x</b>\nhttps://a.example #7", ctx("plain"))
    assert "&lt;b&gt;" in out and 'href="https://a.example"' in out and "remi://issue/7" in out


def test_empty():
    assert render("", ctx()) == "" and render(None, ctx()) == ""


def test_details():
    names = DetailNames(status={1: "新規", 2: "進行中"}, user={42: "岡本"})
    d = describe_detail({"property": "attr", "name": "status_id", "old_value": "1", "new_value": "2"}, names, "ja")
    assert d["text"] == "ステータス を 新規 から 進行中 に変更"
    d = describe_detail({"property": "attr", "name": "assigned_to_id", "old_value": None, "new_value": "42"}, names, "en")
    assert d["text"] == "Assignee set to 岡本"
    d = describe_detail({"property": "attachment", "name": "9", "new_value": "a.png"}, names, "ja")
    assert d["attachmentId"] == 9 and d["filename"] == "a.png"
    d = describe_detail({"property": "relation", "name": "relates", "new_value": "5"}, names, "en")
    assert d["text"] == "Related to #5 added"
    d = describe_detail({"property": "attr", "name": "status_id", "old_value": "1", "new_value": "99"}, names, "en")
    assert "#99" in d["text"]
