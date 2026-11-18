"""本文の中で自動でリンクにするURIスキーム(src/remi/link_schemes.json で指定する。設計書 §5.3)。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMES_FILE = Path(__file__).resolve().parents[1] / "link_schemes.json"
# ファイルに書いてもリンクにしないスキーム(スクリプトの実行・内容の埋め込みに使える)。
# http / https / mailto はもともとリンクになる(Markdownなどの変換で扱う)
BLOCKED = frozenset({"javascript", "data", "vbscript", "about", "blob", "http", "https", "mailto", "remi", "remi-ext"})
_NAME = re.compile(r"^[a-z][a-z0-9+.-]*$")


@dataclass(frozen=True)
class LinkScheme:
    scheme: str
    slashes: bool  # 「スキーム://」の形だけをリンクにする
    confirm: bool  # 開く前に確認する


@lru_cache(maxsize=1)
def link_schemes(path: Path = SCHEMES_FILE) -> tuple[LinkScheme, ...]:
    """指定ファイルを読む。書き方の誤りは記録して、その項目だけを無視する(ファイルがなければ何もリンクにしない)。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except (OSError, ValueError):
        log.warning("cannot read %s", path, exc_info=True)
        return ()
    out: dict[str, LinkScheme] = {}
    for item in data.get("schemes") or []:
        name = str((item or {}).get("scheme", "")).strip().lower()
        form = (item or {}).get("form", "//")
        if not _NAME.match(name) or name in BLOCKED or form not in ("//", ":"):
            log.warning("ignored link scheme %r in %s", item, path)
            continue
        out[name] = LinkScheme(name, form == "//", bool(item.get("confirm", True)))
    return tuple(out.values())


@lru_cache(maxsize=1)
def _pattern() -> re.Pattern[str] | None:
    schemes = link_schemes()
    if not schemes:
        return None
    alts = [re.escape(s.scheme) + ("://" if s.slashes else ":") for s in schemes]
    # 本文はHTMLのエスケープ済み。&lt; &gt; &quot; などの手前で止め、末尾の句読点・括弧はリンクに含めない
    # 途中に含めない文字: 空白・引用符と、全角の句読点・括弧・空白(日本語の文章に続けて書かれることが多いため)
    inner = "\\s<>\"'\u3001\u3002\uff0c\uff0e\u300c\u300d\u300e\u300f\uff08\uff09\u3010\u3011\u3000"
    # 最後の文字に含めない文字: さらに半角の句読点・閉じ括弧
    last = inner + ".,;:!?)\\]"
    entity = r"(?!&(?:lt|gt|quot|#39|#x27);)"
    body = f"(?:{entity}[^{inner}])*(?:{entity}[^{last}])"
    return re.compile(r"(?<![\w./+-])(?:" + "|".join(alts) + ")" + body, re.I)


_TAG = re.compile(r"(<[^>]+>)")
_SKIP = re.compile(r"<(/?)(a|code|pre)\b", re.I)


def link_custom_schemes(html_text: str) -> str:
    """サニタイズ済みのHTMLの文字の部分で、指定したスキームのURIをリンクにする。
    リンク・コード・整形済みテキストの中は変えない。
    """
    pattern = _pattern()
    if pattern is None or ":" not in html_text:
        return html_text
    out = []
    skip = 0
    for part in _TAG.split(html_text):
        if part.startswith("<"):
            m = _SKIP.match(part)
            if m and not part.endswith("/>"):
                skip = max(0, skip + (-1 if m.group(1) else 1))
            out.append(part)
        elif skip:
            out.append(part)
        else:
            out.append(pattern.sub(lambda u: f'<a href="{u.group(0)}" class="ext uri">{u.group(0)}</a>', part))
    return "".join(out)


def scheme_of(url: str) -> LinkScheme | None:
    """URIのスキームが、自動リンクに指定したものなら、その設定を返す。"""
    m = re.match(r"^([a-z][a-z0-9+.-]*):", url.strip(), re.I)
    if not m:
        return None
    name = m.group(1).lower()
    return next((s for s in link_schemes() if s.scheme == name), None)
