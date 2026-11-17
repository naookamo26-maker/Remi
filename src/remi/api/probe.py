"""サーバー登録時の接続テストと、テキスト書式の推測(設計書 §3.1, §5.3)。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from remi.api.client import RedmineClient
from remi.api.errors import ErrorCode, RedmineError

TEXT_FORMATS = ("common_mark", "textile", "plain")

_MD_PATTERNS = [
    re.compile(r"^\s*(```|~~~)", re.M),  # フェンス付きコードブロック
    re.compile(r"!\[[^\]]*\]\([^)]+\)"),  # 画像
    re.compile(r"\[[^\]]+\]\((https?://|/)[^)]+\)"),  # リンク
    re.compile(r"^#{1,6} \S", re.M),  # 見出し
    re.compile(r"\*\*[^*\n]+\*\*"),  # 太字
]
_TEXTILE_PATTERNS = [
    re.compile(r"^h[1-6]\. ", re.M),  # 見出し
    re.compile(r"<pre>|<code>|^bc\. ", re.M),  # コードブロック
    re.compile(r"(?<![\w!])![^\s!][^!\n]*\.(png|jpe?g|gif|bmp|webp)(\([^)]*\))?!", re.I),  # 画像
    re.compile(r"\"[^\"\n]+\":(https?://|/)\S+"),  # リンク
    re.compile(r"%\{[^}]+\}"),  # 文字色など
]


@dataclass
class FormatGuess:
    text_format: str | None  # 推測できなければ None
    markdown_hits: int = 0
    textile_hits: int = 0
    samples: int = 0


def guess_text_format(texts: list[str]) -> FormatGuess:
    md = tx = 0
    for t in texts:
        if not t:
            continue
        md += sum(len(p.findall(t)) for p in _MD_PATTERNS)
        tx += sum(len(p.findall(t)) for p in _TEXTILE_PATTERNS)
    samples = sum(1 for t in texts if t)
    if md == tx == 0:
        return FormatGuess(None, md, tx, samples)
    return FormatGuess("common_mark" if md >= tx else "textile", md, tx, samples)


@dataclass
class ProbeResult:
    user_id: int
    login: str
    name: str
    group_ids: list[int] = field(default_factory=list)
    assigned_count: int = 0
    watched_count: int = 0
    format_guess: FormatGuess | None = None


def probe_server(client: RedmineClient, sample_issues: int = 5) -> ProbeResult:
    """URL・APIキーを検証し、自分のユーザー情報と書式の推測結果を返す。"""
    try:
        user = client.current_user()
    except RedmineError as e:
        # /users/current.json で 403/404 なら、REST APIが無効の可能性が高い
        if e.code in (ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND):
            raise RedmineError(ErrorCode.REST_DISABLED, e.status, e.detail) from e
        raise
    name = " ".join(p for p in (user.get("lastname"), user.get("firstname")) if p) or user.get("login", "")
    result = ProbeResult(
        user_id=int(user["id"]),
        login=user.get("login", ""),
        name=name,
        group_ids=[int(g["id"]) for g in user.get("groups", [])],
    )
    result.assigned_count = client.issue_count(assigned_to_id="me", status_id="open")
    result.watched_count = client.issue_count(watcher_id="me", status_id="open")

    texts: list[str] = []
    recent = client.get_json(
        "/issues.json",
        {"assigned_to_id": "me", "status_id": "*", "sort": "updated_on:desc", "limit": sample_issues},
    ).get("issues", [])
    for issue in recent:
        detail = client.issue(int(issue["id"]), include="journals")
        texts.append(detail.get("description") or "")
        texts.extend(j.get("notes") or "" for j in detail.get("journals", []))
    result.format_guess = guess_text_format(texts)
    return result
