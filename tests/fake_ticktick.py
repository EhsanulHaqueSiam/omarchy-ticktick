"""A stand-in TickTick Open API, served over real HTTP on loopback.

The point of talking real HTTP rather than stubbing `Client` is that everything
between argv and the socket stays in the test: argument parsing, the JSON contract,
the retry layer, throttle detection, the credential store, the local cache and the
outbox. A stub of `Client` would test none of it, and every bug this suite has
actually caught lived in that gap.

Behaviours reproduced here are the ones verified against the live API — including
the ones its documentation gets wrong:

* timestamps come back with milliseconds (``…T18:00:00.000+0000``),
* throttling is an HTTP **500** carrying ``exceed_query_limit``, not a 429,
* the Inbox is missing from ``GET /project`` but answers at ``/project/inbox/data``,
* ``POST /task/filter`` returns every undone task in one response,
* ``POST /task/search`` honours ``keywords`` and ``status`` and ignores the rest,
* a bare ``dueDate`` with no UTC offset is accepted and then silently dropped.

`Fake.fail_next` / `throttle_next` / `offline` let a test bend any of it.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = "test-token-0123456789"

#: TickTick stamps every timestamp with milliseconds; the documented format has none.
STAMP = "%Y-%m-%dT%H:%M:%S.000%z"

_OFFSET_RE = re.compile(r"[+-]\d{4}$")


class Fake:
    """The account behind the server: projects, tasks, tags, and a fault injector."""

    def __init__(self) -> None:
        self.inbox_id = "inbox777"
        self.projects: list[dict] = [
            {"id": "p-work", "name": "Work", "color": "#F18181", "sortOrder": 1,
             "viewMode": "list", "kind": "TASK", "permission": "write"},
            {"id": "p-home", "name": "Home", "color": "#4CAF50", "sortOrder": 2,
             "viewMode": "list", "kind": "TASK", "permission": "write"},
            {"id": "p-old", "name": "Archive", "color": "", "closed": True, "sortOrder": 3},
        ]
        self.tasks: dict[str, dict] = {}
        self.tags: list[dict] = [
            {"name": "urgent", "label": "urgent", "color": "#E24A4A", "sortOrder": 1, "type": 1},
            {"name": "errand", "label": "Errand", "color": "", "parent": "", "sortOrder": 2, "type": 1},
        ]
        self.groups: list[dict] = []

        #: Requests seen, as ``(method, path)`` — lets a test assert on call counts,
        #: which is how the "one request per refresh" promise stays true.
        self.calls: list[tuple[str, str]] = []

        # --- fault injection
        self.offline = False          # every request dies at the socket
        self.fail_next = 0            # answer N requests with a plain 500
        self.throttle_next = 0        # answer N requests with the throttle 500
        self.auth_fails = False       # answer 401 to everything
        self.garbage_next = 0         # answer N requests with non-JSON
        self.slow_next = 0            # unused hook kept for symmetry

    # -- helpers ---------------------------------------------------------

    def add_task(self, **fields) -> dict:
        task = {
            "id": fields.pop("id", uuid.uuid4().hex[:24]),
            "projectId": fields.pop("projectId", self.inbox_id),
            "title": "",
            "priority": 0,
            "status": 0,
            "sortOrder": -len(self.tasks) * 1000,
            "kind": "TEXT",
            "etag": uuid.uuid4().hex[:8],
            "createdTime": "2026-07-01T10:00:00.000+0000",
            "modifiedTime": "2026-07-01T10:00:00.000+0000",
        }
        task.update(fields)
        self.tasks[task["id"]] = task
        return task

    def undone(self) -> list[dict]:
        return [t for t in self.tasks.values() if int(t.get("status") or 0) == 0]

    def project_ids(self) -> set[str]:
        return {p["id"] for p in self.projects} | {self.inbox_id}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    # -- plumbing --------------------------------------------------------

    @property
    def fake(self) -> Fake:
        return self.server.fake  # type: ignore[attr-defined]

    def log_message(self, *args) -> None:
        """Silence the access log; the test output is the only thing worth reading."""

    def _body(self) -> object:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return None

    def _send(self, status: int, payload: object = None, *, raw: str | None = None) -> None:
        if raw is not None:
            body = raw.encode("utf-8")
        elif payload is None:
            body = b""
        else:
            body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _intercept(self) -> bool:
        """Apply any injected fault. True means the response is already sent."""
        fake = self.fake
        if fake.auth_fails:
            self._send(401, {"errorCode": "invalid_token", "errorMessage": "token rejected"})
            return True
        if fake.garbage_next > 0:
            fake.garbage_next -= 1
            self._send(200, raw="<html>not json at all</html>")
            return True
        if fake.throttle_next > 0:
            fake.throttle_next -= 1
            # The real shape: a 500 whose body names the limit. Not a 429.
            self._send(500, {"errorCode": "exceed_query_limit",
                             "errorMessage": "exceed_query_limit"})
            return True
        if fake.fail_next > 0:
            fake.fail_next -= 1
            self._send(500, {"errorCode": "internal", "errorMessage": "boom"})
            return True
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(401, {"errorCode": "invalid_token"})
            return True
        return False

    # -- routing ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self.fake.calls.append(("GET", self.path))
        if self._intercept():
            return
        fake = self.fake
        path = self.path

        if path == "/open/v1/project":
            # The Inbox is deliberately absent, exactly as the live API leaves it.
            self._send(200, fake.projects)
            return
        if path == "/open/v1/project/inbox/data":
            self._send(200, {"project": None, "columns": [],
                             "tasks": [t for t in fake.undone()
                                       if t["projectId"] == fake.inbox_id]})
            return
        if path == "/open/v1/tag":
            self._send(200, fake.tags)
            return
        if path == "/open/v1/project/group":
            self._send(200, fake.groups)
            return

        m = re.fullmatch(r"/open/v1/project/([^/]+)/data", path)
        if m:
            pid = m.group(1)
            project = next((p for p in fake.projects if p["id"] == pid), None)
            if project is None and pid != fake.inbox_id:
                self._send(404, {"errorMessage": "project not found"})
                return
            self._send(200, {"project": project, "columns": [],
                             "tasks": [t for t in fake.undone() if t["projectId"] == pid]})
            return

        m = re.fullmatch(r"/open/v1/project/([^/]+)/task/([^/]+)", path)
        if m:
            task = fake.tasks.get(m.group(2))
            # /project/{id}/data and this path both see undone tasks only.
            if task is None or task["projectId"] != m.group(1) or int(task.get("status") or 0):
                self._send(404, {"errorMessage": "task not found"})
                return
            self._send(200, task)
            return

        m = re.fullmatch(r"/open/v1/project/([^/]+)/column", path)
        if m:
            self._send(200, [])
            return

        self._send(404, {"errorMessage": f"no route for GET {path}"})

    def do_POST(self) -> None:  # noqa: N802
        self.fake.calls.append(("POST", self.path))
        if self._intercept():
            return
        fake = self.fake
        path = self.path
        body = self._body()

        if path == "/open/v1/task/filter":
            wanted = set((body or {}).get("status") or [0])
            tags = set((body or {}).get("tag") or [])
            out = [t for t in fake.tasks.values() if int(t.get("status") or 0) in wanted]
            if tags:
                out = [t for t in out if tags & set(t.get("tags") or [])]
            self._send(200, out)
            return

        if path == "/open/v1/task/search":
            words = str((body or {}).get("keywords") or "").strip().lower()
            if not words:
                self._send(200, [])
                return
            states = (body or {}).get("status")
            out = [t for t in fake.tasks.values() if words in str(t.get("title", "")).lower()]
            if states is not None:
                out = [t for t in out if int(t.get("status") or 0) in set(states)]
            # projectIds / dueFrom / dueTo are accepted and ignored, as observed live.
            self._send(200, out)
            return

        if path == "/open/v1/task/completed":
            self._send(200, [t for t in fake.tasks.values() if int(t.get("status") or 0) == 2])
            return

        if path == "/open/v1/task/move":
            moved = []
            for entry in body if isinstance(body, list) else []:
                task = fake.tasks.get(str(entry.get("taskId")))
                if task is None:
                    continue
                task["projectId"] = str(entry.get("toProjectId") or task["projectId"])
                task["etag"] = uuid.uuid4().hex[:8]
                moved.append({"id": task["id"], "etag": task["etag"]})
            self._send(200, moved)
            return

        if path == "/open/v1/task":
            fields = dict(body or {})
            if not str(fields.get("title") or "").strip():
                self._send(400, {"errorMessage": "title is required"})
                return
            fields.pop("id", None)
            # A date with no UTC offset is accepted and then silently discarded.
            if fields.get("dueDate") and not _OFFSET_RE.search(str(fields["dueDate"])):
                fields["dueDate"] = None
            fields.setdefault("projectId", fake.inbox_id)
            if fields["projectId"] not in fake.project_ids():
                self._send(404, {"errorMessage": "project not found"})
                return
            self._send(200, fake.add_task(**fields))
            return

        m = re.fullmatch(r"/open/v1/task/([^/]+)", path)
        if m:
            task = fake.tasks.get(m.group(1))
            if task is None:
                self._send(404, {"errorMessage": "task not found"})
                return
            fields = dict(body or {})
            if not fields.get("id") or not fields.get("projectId"):
                # The live API rejects an update body missing either of these.
                self._send(400, {"errorMessage": "id and projectId are required"})
                return
            if fields.get("dueDate") and not _OFFSET_RE.search(str(fields["dueDate"])):
                fields["dueDate"] = None
            for key, value in fields.items():
                if key == "items" and isinstance(value, list):
                    # Server-side ids are assigned to items that arrive without one.
                    for item in value:
                        if isinstance(item, dict) and not item.get("id"):
                            item["id"] = uuid.uuid4().hex[:12]
                task[key] = value
            task["etag"] = uuid.uuid4().hex[:8]
            self._send(200, task)
            return

        m = re.fullmatch(r"/open/v1/project/([^/]+)/task/([^/]+)/complete", path)
        if m:
            task = fake.tasks.get(m.group(2))
            if task is None or task["projectId"] != m.group(1):
                self._send(404, {"errorMessage": "task not found"})
                return
            task["status"] = 2
            task["completedTime"] = "2026-07-30T12:00:00.000+0000"
            self._send(200, None)  # 200 with an empty body, as the live API answers
            return

        if path == "/open/v1/project":
            name = str((body or {}).get("name") or "").strip()
            if not name:
                self._send(400, {"errorMessage": "name is required"})
                return
            project = {"id": "p-" + uuid.uuid4().hex[:8], "name": name,
                       "color": str((body or {}).get("color") or ""),
                       "viewMode": "list", "kind": "TASK", "sortOrder": 99}
            fake.projects.append(project)
            self._send(200, project)
            return

        m = re.fullmatch(r"/open/v1/project/([^/]+)", path)
        if m:
            project = next((p for p in fake.projects if p["id"] == m.group(1)), None)
            if project is None:
                self._send(404, {"errorMessage": "project not found"})
                return
            project.update({k: v for k, v in (body or {}).items() if k != "id"})
            self._send(200, project)
            return

        if path == "/open/v1/tag":
            tag = {"name": str((body or {}).get("name") or ""),
                   "label": str((body or {}).get("label") or ""), "sortOrder": 9, "type": 1}
            fake.tags.append(tag)
            self._send(200, tag)
            return

        self._send(404, {"errorMessage": f"no route for POST {path}"})

    def do_DELETE(self) -> None:  # noqa: N802
        self.fake.calls.append(("DELETE", self.path))
        if self._intercept():
            return
        fake = self.fake

        m = re.fullmatch(r"/open/v1/project/([^/]+)/task/([^/]+)", self.path)
        if m:
            task = fake.tasks.get(m.group(2))
            if task is None or task["projectId"] != m.group(1):
                self._send(404, {"errorMessage": "task not found"})
                return
            del fake.tasks[m.group(2)]
            self._send(200, None)
            return

        m = re.fullmatch(r"/open/v1/project/([^/]+)", self.path)
        if m:
            before = len(fake.projects)
            fake.projects = [p for p in fake.projects if p["id"] != m.group(1)]
            if len(fake.projects) == before:
                self._send(404, {"errorMessage": "project not found"})
                return
            fake.tasks = {k: v for k, v in fake.tasks.items() if v["projectId"] != m.group(1)}
            self._send(200, None)
            return

        self._send(404, {"errorMessage": f"no route for DELETE {self.path}"})


class Server:
    """A running fake, as a context manager. `base` is what `Client.BASE` should be."""

    def __init__(self) -> None:
        self.fake = Fake()
        self._httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.fake = self.fake  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/open/v1"

    def __enter__(self) -> "Server":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


__all__ = ["Fake", "Server", "TOKEN"]
