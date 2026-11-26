"""チケット一覧の絞り込み・並べ替え・選択肢の集計(設計書 §5.1)。

一覧の1行は、api.py が作る辞書(item)で表す。ここは、その辞書に対して、
画面から渡された条件(Conditions)を当てはめる。属性は、サーバーごとに違う ID ではなく、名前で持つ
(複数のサーバーを一度に見ても、同じ名前のものとして選べるように)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

STATUS_MODES = ("open", "closed", "all")
SORT_KEYS = ("updated", "number", "subject", "status", "tracker", "priority", "due")
DUE_KEYS = ("overdue", "week", "none")
ME = "@me"  # 担当者の絞り込みの「自分」(グループ経由の担当を含む)
NOBODY = "@none"  # 担当者の絞り込みの「未割り当て」
GROUPS = ("status", "tracker", "priority", "assignee", "due")
POSITION_UNKNOWN = 10**9  # 順序を知らないもの(マスターを取得し直す前)は、最後に置く


def _names(v: Any) -> bool:
    return isinstance(v, list) and len(v) <= 200 and all(isinstance(x, str) and x and len(x) <= 200 for x in v)


def valid_status_filter(v: Any) -> bool:
    return (isinstance(v, dict) and set(v) <= {"mode", "names"} and v.get("mode", "open") in STATUS_MODES
            and _names(v.get("names", [])))


def valid_list_filter(v: Any) -> bool:
    return (isinstance(v, dict) and set(v) <= {"trackers", "priorities", "assignees", "due"}
            and all(_names(v.get(k, [])) for k in ("trackers", "priorities", "assignees"))
            and isinstance(v.get("due", []), list) and all(x in DUE_KEYS for x in v.get("due", [])))


def valid_sort(v: Any) -> bool:
    return isinstance(v, dict) and set(v) <= {"key", "desc"} and v.get("key", "updated") in SORT_KEYS \
        and isinstance(v.get("desc", True), bool)


PRESETS_MAX = 50
PRESET_NAME_MAX = 60
RELATION_VALUES = ("all", "assigned", "watching", "authored")


def valid_presets(v: Any) -> bool:
    """一覧の条件のプリセット(設計書 §5.1)。名前は重複なし。relation が None のものは、関係を変えない(マイルストーンで保存したもの)。"""
    if not isinstance(v, list) or len(v) > PRESETS_MAX:
        return False
    names = set()
    for p in v:
        if not (isinstance(p, dict) and set(p) == {"name", "search", "relation", "status", "filter", "sort"}):
            return False
        name = p["name"]
        if not (isinstance(name, str) and name.strip() == name and 0 < len(name) <= PRESET_NAME_MAX) or name in names:
            return False
        names.add(name)
        if not (isinstance(p["search"], str) and len(p["search"]) <= 200):
            return False
        if not (p["relation"] is None or p["relation"] in RELATION_VALUES):
            return False
        if not (valid_status_filter(p["status"]) and valid_list_filter(p["filter"]) and valid_sort(p["sort"])):
            return False
    return True


@dataclass
class Conditions:
    """一覧に当てる条件。空のものは、絞り込まない。"""

    status_mode: str = "open"
    statuses: list[str] = field(default_factory=list)  # 個別のステータス。あれば status_mode は使わない
    trackers: list[str] = field(default_factory=list)
    priorities: list[str] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    due: list[str] = field(default_factory=list)
    sort: str = "updated"
    desc: bool = True

    def has_attribute_filter(self) -> bool:
        return bool(self.statuses or self.trackers or self.priorities or self.assignees or self.due)

    def selected(self, group: str) -> list[str]:
        return {"status": self.statuses, "tracker": self.trackers, "priority": self.priorities,
                "assignee": self.assignees, "due": self.due}[group]


def due_state(due: str | None, closed: bool, today: date) -> str:
    """期日の区分: overdue(期限切れ。クローズ済みは数えない)/ week(今週)/ none(期日なし)/ other(それ以外)。"""
    if not due:
        return "none"
    try:
        d = date.fromisoformat(due[:10])
    except ValueError:
        return "none"
    if d < today and not closed:
        return "overdue"
    start = today - timedelta(days=today.weekday())  # 今週は、月曜から日曜まで
    if start <= d <= start + timedelta(days=6):
        return "week"
    return "other"


def matches(item: dict[str, Any], cond: Conditions, skip: str | None = None) -> bool:
    """条件に合うか。skip を渡すと、そのグループの条件は無視する(その選択肢の件数を数えるときに使う)。"""
    if skip != "status":
        if cond.statuses:
            if item["status"] not in cond.statuses:
                return False
        elif cond.status_mode == "open" and item["closed"]:
            return False
        elif cond.status_mode == "closed" and not item["closed"]:
            return False
    if skip != "tracker" and cond.trackers and item["tracker"] not in cond.trackers:
        return False
    if skip != "priority" and cond.priorities and item["priority"] not in cond.priorities:
        return False
    if skip != "assignee" and cond.assignees and not _assignee_matches(item, cond.assignees):
        return False
    if skip != "due" and cond.due and item["dueState"] not in cond.due:
        return False
    return True


def _assignee_matches(item: dict[str, Any], wanted: list[str]) -> bool:
    for w in wanted:
        if w == ME:
            if "assigned" in item["relations"] or "group" in item["relations"]:
                return True
        elif w == NOBODY:
            if not item["assignee"]:
                return True
        elif item["assignee"] == w:
            return True
    return False


def sort_items(items: list[dict[str, Any]], cond: Conditions) -> list[dict[str, Any]]:
    """並べ替える。同じ値のものは、更新日時の新しい順。期日のないものは、昇順でも降順でも最後。"""
    items = sorted(items, key=lambda i: i["updatedOn"] or "", reverse=True)  # 同値のときの順(新しい順)
    key = cond.sort
    if key == "updated":
        return items if cond.desc else list(reversed(items))
    if key == "due":
        dated = [i for i in items if i["dueDate"]]
        dated.sort(key=lambda i: i["dueDate"], reverse=cond.desc)
        return dated + [i for i in items if not i["dueDate"]]
    if key in ("status", "tracker", "priority"):
        # Redmine での順序を知らないもの(マスターを取得し直す前)は、昇順でも降順でも最後に置く。
        # 降順を reverse で全体に掛けると、順序を知らないものが先頭に来てしまうので、知っているものだけを並べる
        known = [i for i in items if i["_pos"][key] != POSITION_UNKNOWN]
        unknown = [i for i in items if i["_pos"][key] == POSITION_UNKNOWN]
        known.sort(key=lambda i: (i["_pos"][key], i[key]), reverse=cond.desc)
        unknown.sort(key=lambda i: i[key])
        return known + unknown
    getters = {
        "number": lambda i: i["issueId"],
        "subject": lambda i: (i["subject"].casefold(), i["subject"]),
    }
    return sorted(items, key=getters[key], reverse=cond.desc)


def facets(base: list[dict[str, Any]], cond: Conditions) -> dict[str, list[dict[str, Any]]]:
    """絞り込みの選択肢と件数。いまの一覧(検索・関係の絞り込み後)にあるものだけ。
    各グループの件数は、そのグループ以外の条件を当てたあとの件数(選んだ値を変えたら、何件になるか)。
    選んでいる値は、件数が0でも残す(外せるように)。"""
    out: dict[str, list[dict[str, Any]]] = {}
    for g in GROUPS:
        rows = [i for i in base if matches(i, cond, skip=g)]
        counts: dict[str, int] = {}
        order: dict[str, tuple] = {}
        for i in rows:
            for v in _values(g, i):
                counts[v] = counts.get(v, 0) + 1
                order.setdefault(v, (i["_pos"].get(g, POSITION_UNKNOWN),) if g in ("status", "tracker", "priority") else (0,))
        for v in cond.selected(g):
            counts.setdefault(v, 0)
            order.setdefault(v, (POSITION_UNKNOWN,))
        if g == "assignee":
            keys = sorted(counts, key=lambda v: (v != ME, v != NOBODY, -counts[v], v))
        elif g == "due":
            keys = [k for k in DUE_KEYS if k in counts]
        else:
            keys = sorted(counts, key=lambda v: (order[v], v))
        out[g] = [{"value": v, "count": counts[v]} for v in keys]
    return out


def _values(group: str, item: dict[str, Any]) -> list[str]:
    if group == "status":
        return [item["status"]] if item["status"] else []
    if group == "tracker":
        return [item["tracker"]] if item["tracker"] else []
    if group == "priority":
        return [item["priority"]] if item["priority"] else []
    if group == "assignee":
        # 自分が直接担当のものは「自分」だけ(自分の名前を並べない)。グループ経由は、「自分」とグループ名の両方
        mine = "assigned" in item["relations"] or "group" in item["relations"]
        if mine and "group" not in item["relations"]:
            return [ME]
        out = [ME] if mine else []
        out.append(item["assignee"] if item["assignee"] else NOBODY)
        return out
    return [item["dueState"]] if item["dueState"] in DUE_KEYS else []
