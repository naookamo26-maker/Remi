"""テスト用の偽Redmineサーバー。

実際のRedmine REST APIのうち、Remiが使う部分だけを最小限まねる。
データはメモリ上に持ち、テストから直接書き換えられる。
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

API_KEY = "test-key"
ME = 42


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeRedmine:
    def __init__(self) -> None:
        self.users = {
            ME: {"id": ME, "login": "okamo", "firstname": "ナオ", "lastname": "岡本"},
            7: {"id": 7, "login": "tanaka", "firstname": "一郎", "lastname": "田中"},
            8: {"id": 8, "login": "sato", "firstname": "花子", "lastname": "佐藤"},
        }
        self.groups = [{"id": 100, "name": "開発チーム"}]
        self.projects: list[dict] = []
        self.statuses = [
            {"id": 1, "name": "新規", "is_closed": False},
            {"id": 2, "name": "進行中", "is_closed": False},
            {"id": 5, "name": "終了", "is_closed": True},
        ]
        self.trackers = [{"id": 1, "name": "バグ"}, {"id": 2, "name": "機能"}]
        self.priorities = [{"id": 2, "name": "通常"}, {"id": 3, "name": "高め"}]
        self.issues: dict[int, dict] = {}
        self.journals: dict[int, list[dict]] = {}
        self.attachments: dict[int, dict] = {}  # id -> {..., "content": bytes}
        self.watchers: dict[int, set[int]] = {}
        self.requests: list[tuple[str, str]] = []
        self.rest_enabled = True
        self.fail_next: int | None = None  # 次のリクエストを指定ステータスで失敗させる
        self.fail_next_put: int | None = None  # 次の更新(PUT)だけを失敗させる
        self._next_journal = 1000
        self._next_upload = 0
        self.uploads: dict[str, tuple[str, bytes]] = {}
        self.max_upload = 5 * 1024 * 1024
        self.members: dict[int, list[int]] = {}  # project_id -> user_id
        self.allowed_statuses_supported = True
        self._lock = threading.RLock()

    # --- テスト用のデータ操作 ------------------------------------------------
    def add_project(self, pid: int, name: str, parent: int | None = None, status: int = 1) -> None:
        p = {"id": pid, "name": name, "identifier": f"p{pid}", "status": status}
        if parent is not None:
            pname = next((x["name"] for x in self.projects if x["id"] == parent), f"親{parent}")
            p["parent"] = {"id": parent, "name": pname}
        self.projects.append(p)

    def add_issue(self, iid: int, project_id: int, subject: str, *, assigned_to: int | None = ME,
                  author: int = 7, watchers: set[int] | None = None, status_id: int = 1,
                  description: str = "", updated_on: str | None = None) -> dict:
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
        for up in data.get("uploads") or []:
            filename, content = self.uploads.pop(up["token"])
            aid = 9000 + len(self.attachments)
            self.add_attachment(iid, aid, up.get("filename") or filename, content,
                                up.get("content_type") or "application/octet-stream")
            details.append({"property": "attachment", "name": str(aid), "new_value": up.get("filename") or filename})
        if not notes.strip() and not details:
            return 422, {"errors": ["Notes can't be blank"]}
        self.add_journal(iid, ME, notes, details=details, private=bool(data.get("private_notes")))
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
        me_ids = {ME, *(g["id"] for g in self.groups)}
        if "assigned_to_id" in q:
            a = issue.get("assigned_to", {}).get("id")
            if q["assigned_to_id"] == "me":
                if a not in me_ids:
                    return False
            elif str(a) != q["assigned_to_id"]:
                return False
        if q.get("watcher_id") == "me" and ME not in self.watchers.get(issue["id"], set()):
            return False
        if q.get("author_id") == "me" and issue["author"]["id"] != ME:
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
        upd = q.get("updated_on")
        if upd:
            m = re.match(r"^>=(.+)$", upd)
            if m and issue["updated_on"] < m.group(1):
                return False
        return True

    def handle(self, method: str, path: str, query: dict[str, str], headers, body: bytes):
        with self._lock:
            self.requests.append((method, path + ("?" + "&".join(f"{k}={v}" for k, v in sorted(query.items())) if query else "")))
            if self.fail_next is not None:
                code, self.fail_next = self.fail_next, None
                return code, {"errors": ["forced"]}
            if self.fail_next_put is not None and method == "PUT":
                code, self.fail_next_put = self.fail_next_put, None
                return code, {"errors": ["forced"]}
            if headers.get("X-Redmine-API-Key") != API_KEY:
                return 401, {}
            if not self.rest_enabled:
                return 403, {}

            if path == "/users/current.json":
                u = dict(self.users[ME])
                u["groups"] = self.groups
                return 200, {"user": u}
            if path == "/projects.json":
                return 200, self._page(self.projects, query, "projects")
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
                                                 for x in self.statuses]
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
