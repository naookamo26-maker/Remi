"""左ペインのツリーの組み立て(設計書 §3.2)。"""

from __future__ import annotations

from typing import Any

from remi.context import AppContext
from remi.store.issues import RELATIONS
from remi.store.projects import PROJECT_STATUS_ACTIVE, Project


def visible_projects(projects: list[Project], monitored_project_ids: set[int], show_all: bool) -> set[int]:
    """表示するプロジェクト: 既定は「監視対象のチケットを含むプロジェクトとその祖先」。"""
    by_id = {p.project_id: p for p in projects}
    base = {p.project_id for p in projects if p.accessible and p.status == PROJECT_STATUS_ACTIVE} if show_all else set()
    base |= {pid for pid in monitored_project_ids if pid in by_id}
    out: set[int] = set()
    for pid in base:
        cur: int | None = pid
        while cur is not None and cur in by_id and cur not in out:
            out.add(cur)
            cur = by_id[cur].parent_id
    return out


def build_tree(ctx: AppContext) -> dict[str, Any]:
    # 件数・未読数は、一覧に出すチケット(プロジェクトごとの表示するチケットと絞り込みボタンの選択)だけを数える
    scope = ctx.relation_scope()
    servers_out = []
    total_unread = 0
    for s in ctx.servers.list(enabled_only=True):
        sid = s.server_id
        projects = ctx.projects.list(sid)
        by_id = {p.project_id: p for p in projects}
        unread = ctx.issues.unread_by_project(sid, scope)
        counts = ctx.issues.count_by_project(sid, scope)
        relations = ctx.projects.visible_relations_map(sid)
        own = ctx.projects.own_visible_relations(sid)
        # 表示するプロジェクトは、表示の選択で隠したチケットも含めて決める(選択を変えるたびに
        # ツリーの形が変わらないように)
        monitored = set(ctx.issues.count_by_project(sid))
        visible = visible_projects(projects, monitored, s.show_all_projects)
        children: dict[int | None, list[Project]] = {}
        for pid in visible:
            p = by_id[pid]
            parent = p.parent_id if p.parent_id in visible else None
            children.setdefault(parent, []).append(p)

        def node(p: Project) -> dict[str, Any]:
            kids = [node(c) for c in sorted(children.get(p.project_id, []), key=lambda x: x.name.lower())]
            return {
                "projectId": p.project_id,
                "name": p.name,
                "accessible": p.accessible,
                "active": p.status == PROJECT_STATUS_ACTIVE,
                "unread": unread.get(p.project_id, 0) + sum(k["unread"] for k in kids),
                "count": counts.get(p.project_id, 0) + sum(k["count"] for k in kids),
                # 表示するチケット(継承を解決済み)と、このプロジェクト自身で設定しているか
                "relations": [r for r in RELATIONS if r in relations.get(p.project_id, RELATIONS)],
                "relationsOwn": p.project_id in own,
                "children": kids,
            }

        roots = [node(p) for p in sorted(children.get(None, []), key=lambda x: x.name.lower())]
        s_unread = sum(unread.values())
        total_unread += s_unread
        servers_out.append({
            "serverId": sid,
            "name": s.name,
            "url": s.url,
            "status": "error" if s.last_error else ("ok" if s.last_synced_at else "unknown"),
            "error": s.last_error,
            "lastSyncedAt": s.last_synced_at,
            "unread": s_unread,
            "count": sum(counts.values()),
            "showAllProjects": s.show_all_projects,
            "projects": roots,
        })
    # 一覧の絞り込みボタンは、どこかのプロジェクトで表示している関係のものだけを出す
    return {"unread": total_unread, "servers": servers_out, "relationsInUse": list(scope.in_use)}
