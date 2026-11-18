"""テスト用の偽Redmineサーバー。

実際のRedmine REST APIのうち、Remiが使う部分だけを最小限まねる。
データはメモリ上に持ち、テストから直接書き換えられる。
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

API_KEY = "test-key"
ME = 42


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class WithHeaders:
    """応答の本文に、ヘッダーを添える(テスト用)。"""

    def __init__(self, payload, headers: dict[str, str]) -> None:
        self.payload = payload
        self.headers = headers


class FakeRedmine:
    def __init__(self) -> None:
        self.users = {
            ME: {"id": ME, "login": "okamo", "firstname": "ナオ", "lastname": "岡本"},
            7: {"id": 7, "login": "tanaka", "firstname": "一郎", "lastname": "田中"},
            8: {"id": 8, "login": "sato", "firstname": "花子", "lastname": "佐藤"},
        }
        self.groups = [{"id": 100, "name": "開発チーム"}]  # ME だけが所属する
        # APIキー → ユーザー。リクエストごとに self.me を切り替える(_lock の中で処理するので安全)
        self.keys = {API_KEY: ME}
        self.me = ME
        self.projects: list[dict] = []
        self.statuses = [
            {"id": 1, "name": "新規", "is_closed": False},
            {"id": 2, "name": "進行中", "is_closed": False},
            {"id": 5, "name": "終了", "is_closed": True},
        ]
        self.trackers = [{"id": 1, "name": "バグ"}, {"id": 2, "name": "機能"}]
        self.priorities = [{"id": 2, "name": "通常"}, {"id": 3, "name": "高め"}]
        self.issues: dict[int, dict] = {}
        self.versions: dict[int, dict] = {}
        # カスタムフィールドの定義(/custom_fields.json は管理者だけ)と、プロジェクトのカテゴリ
        self.custom_fields: list[dict] = []
        self.notes_editable = True  # コメントの編集を許すか(False なら 403)
        self.categories: dict[int, list[dict]] = {}
        self.journals: dict[int, list[dict]] = {}
        self.attachments: dict[int, dict] = {}  # id -> {..., "content": bytes}
        self.watchers: dict[int, set[int]] = {}
        self.requests: list[tuple[str, str]] = []
        self.rest_enabled = True
        self.fail_next: int | None = None  # 次のリクエストを指定ステータスで失敗させる
        self.fail_next_put: int | None = None  # 次の更新(PUT)だけを失敗させる
        self.fail_headers: dict[str, str] | None = None  # fail_next の応答に付けるヘッダー(Retry-After など)
        self.delay = 0.0  # 応答を遅らせる秒数(通信中に同期を止めるテスト用)
        self._next_journal = 1000
        self._next_upload = 0
        self.uploads: dict[str, tuple[str, bytes]] = {}
        self.max_upload = 5 * 1024 * 1024
        self.members: dict[int, list[int]] = {}  # project_id -> user_id
        self.allowed_statuses_supported = True
        # 書き込めるかの判定に使う: 自分のメンバーシップ(project_id → ロールID)とロールの権限。
        # my_roles が None なら /users/current.json に memberships を返さない(権限が分からない)
        self.my_roles: dict[int, list[int]] | None = None
        self.roles: dict[int, list[str]] = {}
        self.admin = False
        self.allowed_status_ids: list[int] | None = None  # None ならすべてのステータスに遷移できる
        self._lock = threading.RLock()

    # --- テスト用のデータ操作 ------------------------------------------------
    def add_project(self, pid: int, name: str, parent: int | None = None, status: int = 1) -> None:
        p = {"id": pid, "name": name, "identifier": f"p{pid}", "status": status}
        if parent is not None:
            pname = next((x["name"] for x in self.projects if x["id"] == parent), f"親{parent}")
            p["parent"] = {"id": parent, "name": pname}
        self.projects.append(p)

    def add_version(self, vid: int, project_id: int, name: str, *, status: str = "open",
                    due_date: str | None = None) -> dict:
        with self._lock:
            proj = next((p for p in self.projects if p["id"] == project_id), {"id": project_id, "name": "?"})
            v = {"id": vid, "project": {"id": project_id, "name": proj["name"]}, "name": name, "status": status,
                 "due_date": due_date, "description": "", "sharing": "none",
                 "created_on": now(), "updated_on": now()}
            self.versions[vid] = v
            return v

    def update_version(self, vid: int, **changes) -> None:
        with self._lock:
            self.versions[vid].update(changes, updated_on=now())

    def set_version(self, iid: int, vid: int | None) -> None:
        """チケットの対象バージョンを変える(更新日時も進める)。"""
        with self._lock:
            issue = self.issues[iid]
            if vid is None:
                issue.pop("fixed_version", None)
            else:
                issue["fixed_version"] = {"id": vid, "name": self.versions[vid]["name"]}
            issue["updated_on"] = now()

    def add_issue(self, iid: int, project_id: int, subject: str, *, assigned_to: int | None = ME,
                  author: int = 7, watchers: set[int] | None = None, status_id: int = 1,
                  description: str = "", updated_on: str | None = None, version: int | None = None,
                  custom_fields: list[dict] | None = None) -> dict:
        with self._lock:
            ts = updated_on or now()
            proj = next((p for p in self.projects if p["id"] == project_id), {"id": project_id, "name": "?"})
            issue = {
                "id": iid,
                "project": {"id": project_id, "name": proj["name"]},
                "tracker": {"id": 1, "name": "バグ"},
                "status": self._status(status_id),
                "priority": {"id": 2, "name": "通常"},
                "author": self._user_ref(author),
                "subject": subject,
                "description": description,
                "created_on": ts,
                "updated_on": ts,
                "closed_on": ts if self._status(status_id).get("is_closed") else None,
            }
            if assigned_to is not None:
                issue["assigned_to"] = self._user_ref(assigned_to)
            if version is not None:
                issue["fixed_version"] = {"id": version, "name": self.versions[version]["name"]}
            if custom_fields is not None:
                issue["custom_fields"] = [dict(cf) for cf in custom_fields]
            self.issues[iid] = issue
            self.journals[iid] = []
            self.watchers[iid] = set(watchers or set())
            return issue

    def add_journal(self, iid: int, user: int, notes: str = "", details: list | None = None,
                    private: bool = False, created_on: str | None = None) -> dict:
        with self._lock:
            self._next_journal += 1
            ts = created_on or now()
            j = {
                "id": self._next_journal,
                "user": self._user_ref(user),
                "notes": notes,
                "created_on": ts,
                "updated_on": ts,
                "private_notes": private,
                "details": details or [],
            }
            self.journals[iid].append(j)
            self.issues[iid]["updated_on"] = ts
            return j

    def add_attachment(self, iid: int, aid: int, filename: str, content: bytes,
                       content_type: str = "image/png") -> None:
        self.attachments[aid] = {
            "id": aid, "issue_id": iid, "filename": filename, "filesize": len(content),
            "content_type": content_type, "description": "", "author": self._user_ref(7),
            "created_on": now(), "content": content,
        }

    def set_status(self, iid: int, status_id: int, by: int = 7) -> None:
        old = self.issues[iid]["status"]["id"]
        self.issues[iid]["status"] = self._status(status_id)
        if self._status(status_id).get("is_closed"):
            self.issues[iid]["closed_on"] = now()
        self.add_journal(iid, by, details=[{"property": "attr", "name": "status_id",
                                            "old_value": str(old), "new_value": str(status_id)}])

    def _update(self, iid: int, data: dict) -> tuple[int, Any]:
        """PUT /issues/:id.json: コメント・項目の変更・添付をまとめて1つの journal にする。"""
        notes = data.get("notes", "") or ""
        details = []
        issue = self.issues[iid]
        if "status_id" in data:
            new = self._status(int(data["status_id"]))
            details.append({"property": "attr", "name": "status_id", "old_value": str(issue["status"]["id"]),
                            "new_value": str(new["id"])})
            issue["status"] = new
            if new["is_closed"]:
                issue["closed_on"] = now()
        if "assigned_to_id" in data:
            old = issue.get("assigned_to", {}).get("id")
            new_id = data["assigned_to_id"]
            if new_id in ("", None):
                issue.pop("assigned_to", None)
            else:
                if int(new_id) not in self.users and int(new_id) != 100:
                    return 422, {"errors": ["Assignee is invalid"]}
                issue["assigned_to"] = self._user_ref(int(new_id))
            details.append({"property": "attr", "name": "assigned_to_id", "old_value": str(old) if old else None,
                            "new_value": str(new_id) if new_id not in ("", None) else None})
        if "priority_id" in data:
            details.append({"property": "attr", "name": "priority_id", "old_value": str(issue["priority"]["id"]),
                            "new_value": str(data["priority_id"])})
            issue["priority"] = next(p for p in self.priorities if p["id"] == int(data["priority_id"]))
        if "done_ratio" in data:
            details.append({"property": "attr", "name": "done_ratio", "old_value": str(issue.get("done_ratio", 0)),
                            "new_value": str(data["done_ratio"])})
            issue["done_ratio"] = int(data["done_ratio"])
        # 名前で参照する項目(トラッカー・カテゴリ・対象バージョン・親チケット)と、値をそのまま持つ項目
        refs = {
            "tracker_id": ("tracker", lambda v: next(t for t in self.trackers if t["id"] == v)),
            "category_id": ("category", lambda v: next(c for cs in self.categories.values() for c in cs if c["id"] == v)),
            "fixed_version_id": ("fixed_version", lambda v: {"id": v, "name": self.versions[v]["name"]}),
            "parent_issue_id": ("parent", lambda v: {"id": v} if v in self.issues else None),
        }
        for key, (attr, ref) in refs.items():
            if key not in data:
                continue
            old = (issue.get(attr) or {}).get("id")
            v = None if data[key] in ("", None) else int(data[key])
            if v is None:
                issue.pop(attr, None)
            else:
                r = ref(v)
                if r is None:
                    return 422, {"errors": [f"{key} is invalid"]}
                issue[attr] = r
            details.append({"property": "attr", "name": key, "old_value": None if old is None else str(old),
                            "new_value": None if v is None else str(v)})
        for key in ("start_date", "due_date", "estimated_hours"):
            if key in data:
                v = None if data[key] in ("", None) else data[key]
                details.append({"property": "attr", "name": key, "old_value": issue.get(key), "new_value": v})
                issue[key] = v
        for cf in data.get("custom_fields") or []:
            cur = next((x for x in issue.setdefault("custom_fields", []) if x["id"] == int(cf["id"])), None)
            if cur is None:
                return 422, {"errors": [f"custom field {cf['id']} is invalid"]}
            details.append({"property": "cf", "name": str(cf["id"]), "old_value": cur.get("value"),
                            "new_value": cf["value"]})
            cur["value"] = cf["value"]
        for up in data.get("uploads") or []:
            filename, content = self.uploads.pop(up["token"])
            aid = 9000 + len(self.attachments)
            self.add_attachment(iid, aid, up.get("filename") or filename, content,
                                up.get("content_type") or "application/octet-stream")
            details.append({"property": "attachment", "name": str(aid), "new_value": up.get("filename") or filename})
        if not notes.strip() and not details:
            return 422, {"errors": ["Notes can't be blank"]}
        self.add_journal(iid, self.me, notes, details=details, private=bool(data.get("private_notes")))
        return 204, None

    # --- 内部 -----------------------------------------------------------------
    def _status(self, sid: int) -> dict:
        s = next(x for x in self.statuses if x["id"] == sid)
        return {"id": s["id"], "name": s["name"], "is_closed": s["is_closed"]}

    def _user_ref(self, uid: int) -> dict:
        if uid == 100:
            return {"id": 100, "name": "開発チーム"}
        u = self.users[uid]
        return {"id": uid, "name": f"{u['lastname']} {u['firstname']}"}

    def _match(self, issue: dict, q: dict[str, str]) -> bool:
        me_ids = {self.me, *(g["id"] for g in self.groups if self.me == ME)}
        if "assigned_to_id" in q:
            a = issue.get("assigned_to", {}).get("id")
            if q["assigned_to_id"] == "me":
                if a not in me_ids:
                    return False
            elif str(a) != q["assigned_to_id"]:
                return False
        if q.get("watcher_id") == "me" and self.me not in self.watchers.get(issue["id"], set()):
            return False
        if q.get("author_id") == "me" and issue["author"]["id"] != self.me:
            return False
        status = q.get("status_id", "open")
        if status == "open" and issue["status"]["is_closed"]:
            return False
        if status == "closed" and not issue["status"]["is_closed"]:
            return False
        if "project_id" in q and str(issue["project"]["id"]) != q["project_id"]:
            return False
        if "issue_id" in q and str(issue["id"]) not in q["issue_id"].split(","):
            return False
        if "fixed_version_id" in q and str((issue.get("fixed_version") or {}).get("id")) != q["fixed_version_id"]:
            return False
        upd = q.get("updated_on")
        if upd:
            m = re.match(r"^>=(.+)$", upd)
            if m and issue["updated_on"] < m.group(1):
                return False
        return True

    def handle(self, method: str, path: str, query: dict[str, str], headers, body: bytes):
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            self.requests.append((method, path + ("?" + "&".join(f"{k}={v}" for k, v in sorted(query.items())) if query else "")))
            if self.fail_next is not None:
                code, self.fail_next = self.fail_next, None
                headers_, self.fail_headers = self.fail_headers, None
                return code, WithHeaders({"errors": ["forced"]}, headers_ or {})
            if self.fail_next_put is not None and method == "PUT":
                code, self.fail_next_put = self.fail_next_put, None
                return code, {"errors": ["forced"]}
            me = self.keys.get(headers.get("X-Redmine-API-Key"))
            if me is None:
                return 401, {}
            self.me = me
            if not self.rest_enabled:
                return 403, {}

            if path == "/users/current.json":
                u = dict(self.users[self.me])
                u["groups"] = self.groups if self.me == ME else []
                u["admin"] = self.admin
                if self.my_roles is not None and "memberships" in query.get("include", ""):
                    u["memberships"] = [{"id": i, "project": {"id": pid, "name": ""},
                                         "roles": [{"id": r, "name": f"role{r}"} for r in rids]}
                                        for i, (pid, rids) in enumerate(self.my_roles.items(), 1)]
                return 200, {"user": u}
            m = re.match(r"^/roles/(\d+)\.json$", path)
            if m:
                rid = int(m.group(1))
                if rid not in self.roles:
                    return 404, {}
                return 200, {"role": {"id": rid, "name": f"role{rid}", "permissions": self.roles[rid]}}
            if path == "/projects.json":
                return 200, self._page(self.projects, query, "projects")
            m = re.match(r"^/projects/(\d+)\.json$", path)
            if m:
                p = next((x for x in self.projects if x["id"] == int(m.group(1))), None)
                if p is None:
                    return 404, {}
                p = dict(p)
                inc = query.get("include", "").split(",")
                if "trackers" in inc:
                    p["trackers"] = [{"id": t["id"], "name": t["name"]} for t in self.trackers]
                if "issue_categories" in inc:
                    p["issue_categories"] = self.categories.get(p["id"], [])
                return 200, {"project": p}
            if path == "/custom_fields.json":
                if not self.admin:
                    return 403, {}
                return 200, {"custom_fields": self.custom_fields}
            m = re.match(r"^/projects/(\d+)/versions\.json$", path)
            if m:
                pid = int(m.group(1))
                if not any(p["id"] == pid for p in self.projects):
                    return 404, {}
                items = [v for v in self.versions.values() if v["project"]["id"] == pid or v["sharing"] == "system"]
                return 200, {"versions": items, "total_count": len(items)}
            if path == "/issue_statuses.json":
                return 200, {"issue_statuses": self.statuses}
            if path == "/trackers.json":
                return 200, {"trackers": self.trackers}
            if path == "/enumerations/issue_priorities.json":
                return 200, {"issue_priorities": self.priorities}
            if path == "/issues.json":
                items = [i for i in self.issues.values() if self._match(i, query)]
                sort = query.get("sort", "")
                items.sort(key=lambda i: i["updated_on"], reverse=sort.endswith(":desc"))
                return 200, self._page(items, query, "issues")
            m = re.match(r"^/journals/(\d+)\.json$", path)
            if m and method == "PUT":
                jid = int(m.group(1))
                for iid, js in self.journals.items():
                    j = next((x for x in js if x["id"] == jid), None)
                    if j is None:
                        continue
                    if not self.notes_editable:
                        return 403, {}
                    data = json.loads(body or b"{}").get("journal", {})
                    j["notes"] = data.get("notes", j.get("notes", ""))
                    if "private_notes" in data:
                        j["private_notes"] = bool(data["private_notes"])
                    if not j["notes"] and not j.get("details"):
                        js.remove(j)  # 本文を空にすると、項目の変更がない journal は消える(Redmineと同じ)
                    return 204, None
                return 404, {}
            m = re.match(r"^/attachments/(\d+)\.json$", path)
            if m and method == "DELETE":
                aid = int(m.group(1))
                a = self.attachments.pop(aid, None)
                if a is None:
                    return 404, {}
                self.add_journal(a["issue_id"], self.me, details=[
                    {"property": "attachment", "name": str(aid), "old_value": a["filename"], "new_value": None}])
                return 204, None
            m = re.match(r"^/issues/(\d+)\.json$", path)
            if m:
                iid = int(m.group(1))
                if iid not in self.issues:
                    return 404, {}
                if method == "PUT":
                    return self._update(iid, json.loads(body or b"{}").get("issue", {}))
                issue = dict(self.issues[iid])
                inc = query.get("include", "").split(",")
                if "journals" in inc:
                    issue["journals"] = self.journals[iid]
                if "attachments" in inc:
                    issue["attachments"] = [
                        {k: v for k, v in a.items() if k not in ("content", "issue_id")}
                        | {"content_url": f"{self.base_url}/attachments/download/{a['id']}/{a['filename']}"}
                        for a in self.attachments.values() if a["issue_id"] == iid
                    ]
                if "watchers" in inc:
                    issue["watchers"] = [self._user_ref(u) for u in self.watchers[iid]]
                if "allowed_statuses" in inc and self.allowed_statuses_supported:
                    issue["allowed_statuses"] = [{"id": x["id"], "name": x["name"], "is_closed": x["is_closed"]}
                                                 for x in self.statuses
                                                 if self.allowed_status_ids is None or x["id"] in self.allowed_status_ids]
                return 200, {"issue": issue}
            if path == "/uploads.json" and method == "POST":
                if len(body) > self.max_upload:
                    return 422, {"errors": ["This file cannot be uploaded because it exceeds the maximum allowed file size"]}
                self._next_upload += 1
                token = f"{self._next_upload}.tok"
                self.uploads[token] = (query.get("filename", "file"), body)
                return 201, {"upload": {"id": self._next_upload, "token": token}}
            m = re.match(r"^/projects/(\d+)/memberships\.json$", path)
            if m:
                items = [{"id": i, "project": {"id": int(m.group(1))}, "user": self._user_ref(uid), "roles": []}
                         for i, uid in enumerate(self.members.get(int(m.group(1)), []), 1)]
                return 200, self._page(items, query, "memberships")
            m = re.match(r"^/issues/(\d+)/watchers(?:/(\d+))?\.json$", path)
            if m:
                iid = int(m.group(1))
                if iid not in self.issues:
                    return 404, {}
                if method == "POST":
                    self.watchers[iid].add(int(json.loads(body or b"{}")["user_id"]))
                    return 204, None
                if method == "DELETE":
                    self.watchers[iid].discard(int(m.group(2)))
                    return 204, None
            m = re.match(r"^/attachments/download/(\d+)/", path)
            if m:
                a = self.attachments.get(int(m.group(1)))
                if not a:
                    return 404, {}
                return 200, (a["content"], a["content_type"])
            return 404, {}

    @staticmethod
    def _page(items: list, query: dict[str, str], key: str) -> dict:
        offset = int(query.get("offset", 0))
        limit = min(int(query.get("limit", 25)), 100)
        return {key: items[offset:offset + limit], "total_count": len(items), "offset": offset, "limit": limit}

    # --- サーバー起動 ----------------------------------------------------------
    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: D401 - 静かにする
                pass

            def _do(self, method: str) -> None:
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                code, payload = fake.handle(method, u.path, q, self.headers, body)
                extra: dict[str, str] = {}
                if isinstance(payload, WithHeaders):
                    payload, extra = payload.payload, payload.headers
                if isinstance(payload, tuple):
                    content, ctype = payload
                    self.send_response(code)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                    return
                data = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(code)
                for k, v in extra.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                self._do("GET")

            def do_PUT(self):  # noqa: N802
                self._do("PUT")

            def do_POST(self):  # noqa: N802
                self._do("POST")

            def do_DELETE(self):  # noqa: N802
                self._do("DELETE")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self.base_url

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
