"""コメント本文のレンダリング(設計書 §5.3)。

本文(生テキスト)
  → コードブロックを取り出してプレースホルダーに置き換える
  → コード以外の部分で、Redmine独自記法をプレースホルダーに置き換える
  → Markdown / Textile / プレーンテキストとしてHTMLに変換する
  → nh3 でサニタイズする(許可したタグ・属性以外を取り除く)
  → プレースホルダーを、リンク・ハイライト済みのコードなどのHTMLに戻す
"""

from __future__ import annotations

import html
import re
import secrets
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import quote

import nh3
import textile
from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

from remi.render.schemes import link_custom_schemes, link_schemes, scheme_of

TEXT_FORMATS = ("common_mark", "textile", "plain")


@dataclass
class RenderContext:
    server_id: int
    server_url: str
    text_format: str = "common_mark"
    attachments: dict[str, int] = field(default_factory=dict)  # ファイル名(小文字) -> attachment_id
    my_login: str | None = None
    project_identifier: str | None = None
    allow_external_images: bool = False
    user_names: dict[int, str] = field(default_factory=dict)
    pending: dict[str, str] = field(default_factory=dict)  # 送信前の添付: ファイル名(小文字) -> URL

    def attachment_id(self, name: str) -> int | None:
        return self.attachments.get(name.strip().lower())

    def attachment_url(self, aid: int) -> str:
        return f"/attachments/{self.server_id}/{aid}"

    def resolve(self, name: str) -> str | None:
        """ファイル名から画像・添付のURLを引く(送信前の添付を優先)。"""
        key = name.strip().lower()
        if key in self.pending:
            return self.pending[key]
        aid = self.attachments.get(key)
        return self.attachment_url(aid) if aid is not None else None


# --- サニタイズの設定 -----------------------------------------------------------
ALLOWED_TAGS = {
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "em", "i", "u", "s", "del", "ins",
    "strike", "code", "pre", "blockquote", "ul", "ol", "li", "table", "thead", "tbody", "tfoot", "tr", "th",
    "td", "a", "img", "span", "div", "sup", "sub", "dl", "dt", "dd", "abbr", "cite", "small", "mark", "kbd",
    "caption", "acronym",
}
ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "span": {"style"},
    "p": {"style"},
    "td": {"colspan", "rowspan", "style"},
    "th": {"colspan", "rowspan", "style"},
    "ol": {"start"},
    "abbr": {"title"},
    "acronym": {"title"},
}
ALLOWED_STYLES = {"color", "background-color", "text-align", "font-weight", "font-style", "text-decoration"}

_EXT_IMG = "remi-ext:"


def _attribute_filter(ctx: RenderContext):
    def f(tag: str, attr: str, value: str) -> str | None:
        if tag == "img" and attr == "src":
            v = value.strip()
            if re.match(r"^https?://", v, re.I):
                return _EXT_IMG + v
            return ctx.resolve(v.rsplit("/", 1)[-1])  # 見つからない添付やその他のURLは読み込まない
        if tag == "a" and attr == "href":
            v = value.strip()
            if scheme_of(v) is not None:
                return v  # 自動リンクに指定したスキーム(明示したリンク)はそのまま
            if not re.match(r"^(https?://|mailto:|remi://|/|#)", v, re.I):
                # 相対リンク(Redmine内のパス)はサーバーのURLに結びつける
                url = ctx.resolve(v)
                if url is not None:
                    return url
                return ctx.server_url.rstrip("/") + "/" + v.lstrip("./")
            if v.startswith("/") and not v.startswith("/attachments/"):
                return ctx.server_url.rstrip("/") + v
        return value

    return f


# --- コードブロック ---------------------------------------------------------------
_FENCE = re.compile(r"^(?P<fence>`{3,}|~{3,})[ \t]*(?P<lang>[^\n`]*)\n(?P<code>.*?)(?:^(?P=fence)[ \t]*$|\Z)",
                    re.M | re.S)
_PRE = re.compile(r"<pre>\s*(?:<code(?:\s+class=\"(?:language-)?(?P<lang>[\w+#.-]+)\")?>)?(?P<code>.*?)"
                  r"(?:</code>\s*)?</pre>", re.S | re.I)
_TEXTILE_BC = re.compile(r"^bc\.(?P<ext>\.)?(?:\((?P<lang>[\w+#.-]+)\))? (?P<code>.*?)(?=\n\s*\n|\Z)", re.M | re.S)


@lru_cache(maxsize=64)
def _lexer(lang: str):
    try:
        return get_lexer_by_name(lang)
    except ClassNotFound:
        return None


def highlight_code(code: str, lang: str | None) -> str:
    lang = (lang or "").strip().split()[0].lower() if lang else ""
    lexer = _lexer(lang) if lang else None
    if lexer is None:
        return f'<pre class="code"><code>{html.escape(code)}</code></pre>'
    body = highlight(code, lexer, HtmlFormatter(nowrap=True))
    return f'<pre class="code hl" data-lang="{html.escape(lang)}"><code>{body}</code></pre>'


def highlight_css(style: str, scope: str) -> str:
    return HtmlFormatter(style=style).get_style_defs(f"{scope} .hl")


# --- Redmine独自記法 ----------------------------------------------------------------
_INLINE_CODE = {
    "common_mark": re.compile(r"(`+)(?:.+?)\1", re.S),
    "textile": re.compile(r"@[^@\n]+@|<code>.*?</code>", re.S | re.I),
    "plain": re.compile(r"(?!x)x"),
}

_REF = re.compile(
    r"""
    !(?P<escaped>\#{1,3}\d+(?:\#note-\d+)?|\#note-\d+|r\d+|@[\w.\-]+|attachment:\S+|commit:\w+|user[\#:]\S+
                  |(?:document|version|news)\#\d+)
  | (?<![\w&/\#])(?P<hashes>\#{1,3})(?P<issue>\d+)(?:\#note-(?P<inote>\d+))?\b
  | (?<![\w&/\#])\#note-(?P<note>\d+)\b
  | (?<![\w@.\-])@(?P<mention>[A-Za-z0-9_][A-Za-z0-9_.\-]*[A-Za-z0-9_])
  | \buser\#(?P<userid>\d+)\b
  | \buser:(?P<userlogin>[A-Za-z0-9_.\-]+)
  | \battachment:(?:"(?P<attq>[^"\n]+)"|(?P<att>[^\s<>"']+))
  | (?<![\w/])r(?P<rev>\d+)\b
  | \bcommit:(?P<commit>[0-9a-fA-F]{6,40})\b
  | \[\[(?P<wiki>[^\]\n|]+)(?:\|(?P<wikitext>[^\]\n]+))?\]\]
  | \b(?P<objkind>document|version|news)\#(?P<objid>\d+)\b
    """,
    re.X,
)

_MACRO_COLLAPSE = re.compile(r"\{\{collapse(?:\((?P<title>[^)\n]*)\))?\s*\n?(?P<body>.*?)\}\}", re.S)
_MACRO = re.compile(r"\{\{(?P<name>[A-Za-z_][\w-]*)(?:\([^)]*\))?(?:\s*\n.*?)?\}\}", re.S)


class _Placeholders:
    def __init__(self) -> None:
        self.nonce = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(8))
        self.items: list[tuple[str, bool]] = []  # (html, block)

    def add(self, snippet: str, block: bool = False) -> str:
        self.items.append((snippet, block))
        return f"RMX{self.nonce}P{len(self.items) - 1}Q"

    def restore(self, out: str) -> str:
        pat = re.compile(rf"(?:<p>\s*)?RMX{self.nonce}P(\d+)Q(?:\s*</p>)?")

        # インライン要素は <p> を残し、ブロック要素は <p> ごと置き換える
        def sub2(m: re.Match) -> str:
            snippet, block = self.items[int(m.group(1))]
            if block:
                return snippet
            whole = m.group(0)
            prefix = "<p>" if whole.startswith("<p>") else ""
            suffix = "</p>" if whole.endswith("</p>") else ""
            return f"{prefix}{snippet}{suffix}"

        for _ in range(3):  # 入れ子(折りたたみの中のコードなど)にも対応する
            new = pat.sub(sub2, out)
            if new == out:
                break
            out = new
        return out


def _link(href: str, text: str, cls: str = "") -> str:
    c = f' class="{cls}"' if cls else ""
    return f'<a href="{html.escape(href, quote=True)}"{c}>{html.escape(text)}</a>'


def _ref_html(m: re.Match, ctx: RenderContext, ph: _Placeholders) -> str:
    g = m.groupdict()
    base = ctx.server_url.rstrip("/")
    proj = ctx.project_identifier
    if g["escaped"] is not None:
        return ph.add(html.escape(g["escaped"]))  # 「!」で始まる記法はリンクにしない
    if g["issue"]:
        iid = g["issue"]
        note = g["inote"]
        href = f"remi://issue/{iid}" + (f"#note-{note}" if note else "")
        text = f"#{iid}" + (f"#note-{note}" if note else "")
        return ph.add(_link(href, text, "issue-ref"))
    if g["note"]:
        return ph.add(_link(f"remi://note/{g['note']}", f"#note-{g['note']}", "note-ref"))
    if g["mention"]:
        login = g["mention"]
        me = " me" if ctx.my_login and login.lower() == ctx.my_login.lower() else ""
        return ph.add(f'<span class="mention{me}">@{html.escape(login)}</span>')
    if g["userid"]:
        name = ctx.user_names.get(int(g["userid"]), f"user#{g['userid']}")
        return ph.add(f'<span class="mention">@{html.escape(name)}</span>')
    if g["userlogin"]:
        login = g["userlogin"]
        me = " me" if ctx.my_login and login.lower() == ctx.my_login.lower() else ""
        return ph.add(f'<span class="mention{me}">@{html.escape(login)}</span>')
    if g["attq"] or g["att"]:
        name = g["attq"] or g["att"]
        url = ctx.resolve(name)
        if url is None:
            return ph.add(html.escape(m.group(0)))
        if re.search(r"\.(png|jpe?g|gif|bmp|webp)$", name, re.I):
            return ph.add(f'<a href="{url}" class="attachment image">'
                          f'<img src="{url}" alt="{html.escape(name)}" loading="lazy"></a>')
        return ph.add(_link(url, name, "attachment"))
    if g["rev"]:
        href = f"{base}/projects/{quote(proj)}/repository/revisions/{g['rev']}" if proj else f"{base}/"
        return ph.add(_link(href, m.group(0), "ext"))
    if g["commit"]:
        href = f"{base}/projects/{quote(proj)}/repository/revisions/{g['commit']}" if proj else f"{base}/"
        return ph.add(_link(href, m.group(0), "ext"))
    if g["wiki"]:
        page = g["wiki"].strip()
        target_proj = proj
        if ":" in page:
            target_proj, page = page.split(":", 1)
        href = (f"{base}/projects/{quote(target_proj)}/wiki/{quote(page.replace(' ', '_'))}"
                if target_proj else f"{base}/")
        return ph.add(_link(href, (g["wikitext"] or g["wiki"]).strip(), "ext"))
    if g["objkind"]:
        kinds = {"document": "documents", "version": "versions", "news": "news"}
        return ph.add(_link(f"{base}/{kinds[g['objkind']]}/{g['objid']}", m.group(0), "ext"))
    return m.group(0)


def _replace_refs(text: str, ctx: RenderContext, ph: _Placeholders) -> str:
    inline = _INLINE_CODE.get(ctx.text_format, _INLINE_CODE["plain"])
    out: list[str] = []
    pos = 0
    for m in inline.finditer(text):
        out.append(_REF.sub(lambda r: _ref_html(r, ctx, ph), text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_REF.sub(lambda r: _ref_html(r, ctx, ph), text[pos:]))
    return "".join(out)


# --- 書式ごとの変換 ----------------------------------------------------------------
@lru_cache(maxsize=1)
def _markdown() -> MarkdownIt:
    # Redmine の CommonMark は改行をそのまま <br> にする(hardbreaks)。
    # 文中のURLのリンクは、どの書式でも同じ規則にするため、変換の後で行う(schemes.link_custom_schemes)
    return MarkdownIt("commonmark", {"breaks": True, "html": True}).enable(["table", "strikethrough"])


def _plain_to_html(text: str) -> str:
    # URLのリンクは変換の後で行う(schemes.link_custom_schemes)
    escaped = html.escape(text, quote=False)
    paras = [p.replace("\n", "<br>") for p in re.split(r"\n\s*\n", escaped.strip())]
    return "".join(f"<p>{p}</p>" for p in paras if p)


def _to_html(text: str, fmt: str) -> str:
    if fmt == "common_mark":
        text = re.sub(r"^(\s*(?:[-*+]|\d+[.)])\s+)\[( |x|X)\]\s", lambda m: m.group(1) + ("☑ " if m.group(2) != " " else "☐ "),
                      text, flags=re.M)
        return _markdown().render(text)
    if fmt == "textile":
        return textile.textile(text, html_type="html5")
    return _plain_to_html(text)


def _extract_code(text: str, fmt: str, ph: _Placeholders) -> str:
    def block(code: str, lang: str | None) -> str:
        return "\n\n" + ph.add(highlight_code(code.rstrip("\n"), lang), block=True) + "\n\n"

    if fmt == "common_mark":
        text = _FENCE.sub(lambda m: block(m.group("code"), m.group("lang")), text)
    if fmt == "textile":
        text = _TEXTILE_BC.sub(lambda m: block(m.group("code"), m.group("lang")), text)
    if fmt in ("common_mark", "textile"):
        text = _PRE.sub(lambda m: block(html.unescape(m.group("code")), m.group("lang")), text)
    return text


def _extract_macros(text: str, ctx: RenderContext, ph: _Placeholders, depth: int) -> str:
    def collapse(m: re.Match) -> str:
        title = (m.group("title") or "").strip() or "…"
        inner = render(m.group("body"), ctx, _depth=depth + 1) if depth < 3 else html.escape(m.group("body"))
        return "\n\n" + ph.add(f'<details class="collapse"><summary>{html.escape(title)}</summary>{inner}</details>',
                               block=True) + "\n\n"

    text = _MACRO_COLLAPSE.sub(collapse, text)
    return _MACRO.sub(lambda m: ph.add(f'<span class="macro">{{{{{html.escape(m.group("name"))}}}}}</span>'), text)


def _finish_images(out: str, ctx: RenderContext) -> str:
    def sub(m: re.Match) -> str:
        url = html.unescape(m.group(1))
        if ctx.allow_external_images:
            return f'<img src="/external-image?url={quote(url, safe="")}" alt="" loading="lazy">'
        return _link(url, url, "ext external-image")

    out = re.sub(r'<img[^>]*?src="' + _EXT_IMG + r'([^"]*)"[^>]*>', sub, out)

    def missing(m: re.Match) -> str:
        alt = re.search(r'alt="([^"]*)"', m.group(0))
        return f'<span class="missing-image">[{alt.group(1) if alt and alt.group(1) else "image"}]</span>'

    # 見つからない添付の画像は、説明文だけを表示する
    out = re.sub(r'<img(?![^>]*\ssrc=)[^>]*>', missing, out)
    # 添付画像はクリックで拡大できるようにリンクで包む
    return re.sub(r'(?<!class="attachment image">)(<img [^>]*src="(/(?:attachments/\d+|pending)/\d+)"[^>]*>)',
                  r'<a href="\2" class="attachment image">\1</a>', out)


def render(text: str | None, ctx: RenderContext, *, _depth: int = 0) -> str:
    """本文をサニタイズ済みのHTMLにする。"""
    if not text or not text.strip():
        return ""
    fmt = ctx.text_format if ctx.text_format in TEXT_FORMATS else "plain"
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    ph = _Placeholders()
    text = _extract_code(text, fmt, ph)
    text = _extract_macros(text, ctx, ph, _depth)
    text = _replace_refs(text, ctx, ph)
    raw = _to_html(text, fmt)
    clean = nh3.clean(
        raw,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        attribute_filter=_attribute_filter(ctx),
        url_schemes={"http", "https", "mailto", "remi", "remi-ext", *(x.scheme for x in link_schemes())},
        filter_style_properties=ALLOWED_STYLES,
        link_rel="noopener noreferrer",
    )
    clean = _finish_images(clean, ctx)
    return link_custom_schemes(ph.restore(clean)).strip()
