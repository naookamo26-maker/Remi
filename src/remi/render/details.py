"""journal の変更履歴(details)を、表示用の文にする(設計書 §5.2, §10.2)。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from remi.i18n import t

# 属性名 -> 翻訳キー
ATTR_KEYS = {
    "status_id": "field.status",
    "assigned_to_id": "field.assignee",
    "priority_id": "field.priority",
    "tracker_id": "field.tracker",
    "subject": "field.subject",
    "description": "field.description",
    "done_ratio": "field.doneRatio",
    "start_date": "field.startDate",
    "due_date": "field.dueDate",
    "estimated_hours": "field.estimatedHours",
    "project_id": "field.project",
    "category_id": "field.category",
    "fixed_version_id": "field.version",
    "parent_id": "field.parent",
    "is_private": "field.private",
}
# マスタで名前を引ける属性
MASTER_KINDS = {
    "status_id": "status",
    "assigned_to_id": "user",
    "priority_id": "priority",
    "tracker_id": "tracker",
    "project_id": "project",
}


@dataclass
class DetailNames:
    status: dict[int, str] = field(default_factory=dict)
    user: dict[int, str] = field(default_factory=dict)
    priority: dict[int, str] = field(default_factory=dict)
    tracker: dict[int, str] = field(default_factory=dict)
    project: dict[int, str] = field(default_factory=dict)
    custom_field: dict[int, str] = field(default_factory=dict)

    def lookup(self, kind: str, value: Any) -> str:
        if value in (None, ""):
            return ""
        try:
            key = int(value)
        except (TypeError, ValueError):
            return str(value)
        return getattr(self, kind).get(key, f"#{key}")


def describe_detail(d: dict[str, Any], names: DetailNames, lang: str) -> dict[str, Any]:
    """1件の変更を {"text": ..., "kind": ...} にする。"""
    prop, name = d.get("property"), str(d.get("name", ""))
    old, new = d.get("old_value"), d.get("new_value")

    if prop == "attachment":
        if new:
            return {"kind": "attachment", "attachmentId": _int(name), "filename": new,
                    "text": t("details.attachmentAdded", lang, name=new)}
        return {"kind": "attachment-removed", "text": t("details.attachmentRemoved", lang, name=old or "")}
    if prop == "relation":
        rel = t(f"relation.{name}", lang)
        if new:
            return {"kind": "relation", "issueId": _int(new), "text": t("details.relationAdded", lang, relation=rel, target=f"#{new}")}
        return {"kind": "relation", "issueId": _int(old), "text": t("details.relationRemoved", lang, relation=rel, target=f"#{old}")}

    if prop == "cf":
        field_name = names.lookup("custom_field", name)
        old_s, new_s = old or "", new or ""
    elif prop == "attr":
        field_name = t(ATTR_KEYS[name], lang) if name in ATTR_KEYS else name
        if name in ("description",):
            return {"kind": "attr", "text": t("details.descriptionUpdated", lang)}
        kind = MASTER_KINDS.get(name)
        if kind:
            old_s, new_s = names.lookup(kind, old), names.lookup(kind, new)
        elif name == "parent_id":
            old_s, new_s = (f"#{old}" if old else ""), (f"#{new}" if new else "")
        elif name == "is_private":
            yes, no = t("common.yes", lang), t("common.no", lang)
            old_s = (yes if str(old) == "1" else no) if old is not None else ""
            new_s = (yes if str(new) == "1" else no) if new is not None else ""
        elif name == "done_ratio":
            old_s, new_s = (f"{old}%" if old else ""), (f"{new}%" if new else "")
        else:
            old_s, new_s = (str(old) if old is not None else ""), (str(new) if new is not None else "")
    else:
        field_name, old_s, new_s = name, str(old or ""), str(new or "")

    if old_s and new_s:
        text = t("details.changed", lang, field=field_name, old=old_s, new=new_s)
    elif new_s:
        text = t("details.set", lang, field=field_name, new=new_s)
    else:
        text = t("details.cleared", lang, field=field_name, old=old_s)
    return {"kind": prop or "other", "text": text}


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
