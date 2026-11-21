"""サーバー登録時の接続テストと、テキスト書式の推測(設計書 §3.1, §5.3)。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from remi.api.client import RedmineClient, normalize_url
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


# Redmine の画面のURLに出てくる、決まった部分。ここより手前が、Redmine のトップのアドレス
_REDMINE_PATHS = frozenset({
    "projects", "issues", "versions", "users", "my", "login", "logout", "account", "admin", "groups",
    "time_entries", "activity", "search", "news", "boards", "wiki", "documents", "files", "attachments", "queries",
})
MAX_DISCOVERY_DEPTH = 6  # 区切り(/)の何階層目まで試すか


def candidate_roots(url: str) -> list[str]:
    """入力されたURLから、Redmine のトップのアドレスの候補を、試す順に返す(重複なし)。

    入力されたまま → Redmine の画面のURLの決まった部分(/projects/ など)の手前 → 区切り(/)ごとに、短い方から。
    サーバー登録で、プロジェクトのURLなどを入れてしまった人のため。検索部分(?...)・#... は捨てる。
    """
    parts = urlsplit(normalize_url(url))
    root = f"{parts.scheme}://{parts.netloc}"
    segs = [x for x in parts.path.split("/") if x][:MAX_DISCOVERY_DEPTH]

    def join(n: int) -> str:
        return "/".join([root, *segs[:n]])

    order = [len(segs)]
    marker = next((i for i, x in enumerate(segs) if x.lower() in _REDMINE_PATHS), None)
    if marker is not None:
        order.append(marker)
    order.extend(range(len(segs) + 1))
    return list(dict.fromkeys(join(n) for n in order))


def find_redmine_root(url: str, api_key: str, *, proxy_mode: str = "system", proxy_url: str | None = None) -> str:
    """入力されたURLから、Redmine のトップのアドレスを探す。

    候補ごとに /users/current.json を試し、Redmine の応答(ユーザー)が返ったものを採用する。
    404・403・JSONでない応答のときは、次の候補を試す。接続できない・証明書・タイムアウトなど、
    ホストそのものの失敗は、そのまま返す(ほかの候補でも同じため)。見つからなければ、入力されたまま返す
    (接続テストが、その理由を返す)。APIキーを送るのは、入力されたホストの中だけ(client が検査する)。
    """
    candidates = candidate_roots(url)
    key_rejected: str | None = None  # キーが拒否された=Redmine があった(ほかに見つからなければ、ここを使う)
    for cand in candidates:
        client = RedmineClient(cand, api_key, proxy_mode=proxy_mode, proxy_url=proxy_url, min_interval=0)
        try:
            if "user" in client.get_json("/users/current.json"):
                return client.base_url
        except RedmineError as e:
            if e.code == ErrorCode.UNAUTHORIZED:
                key_rejected = key_rejected or cand
            elif e.code not in (ErrorCode.NOT_FOUND, ErrorCode.FORBIDDEN, ErrorCode.INVALID_RESPONSE,
                                ErrorCode.UNPROCESSABLE, ErrorCode.SERVER_ERROR):
                raise
        finally:
            client.close()
    return key_rejected or candidates[0]


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
        # /users/current.json で 403/404 や、JSONでない応答なら、URLの誤りか、REST APIが無効の可能性が高い
        if e.code in (ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND, ErrorCode.INVALID_RESPONSE):
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
