"""Thin typed wrapper over the TickTick Open API.

One class, one method per documented endpoint.

The primary read path is `all_undone()`, which is a single `POST /task/filter` returning every
undone task across every project including the Inbox. The widely repeated claim that TickTick
has no "all my tasks" endpoint is out of date — `/task/filter` was added to the API and verified
live. The per-project fan-out (`project_data_many`) survives only as a fallback, because fanning
out is precisely what trips TickTick's undocumented rate limiter.

Everything here raises `ticktick.errors` types; nothing here prints or retries a mutation.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import __version__
from . import config as _config
from .errors import ApiError, AuthError, NetworkError, NotFoundError

# Some APIs reject the default "Python-urllib/3.x" UA outright; identify the plugin instead.
USER_AGENT = (
    f"omarchy-ticktick/{__version__} "
    "(+https://github.com/EhsanulHaqueSiam/omarchy-ticktick)"
)

# Backoff for idempotent GETs: 3 attempts total. The jitter is derived from the attempt
# number rather than `random` so a failing run is byte-for-byte reproducible in tests.
_RETRY_DELAYS = (0.5, 1.0)
_JITTER_STEP = 0.05

_MAX_BODY_CHARS = 500  # error bodies are the only way to diagnose TickTick 400s; keep some

# TickTick signals throttling with `exceed_query_limit` in the body — and usually an HTTP 500,
# not a 429. Status alone is not enough to recognise it, so the body has to be sniffed.
_THROTTLE_MARKER = "exceed_query_limit"
_THROTTLE_DELAYS = (2.0, 5.0, 12.0)

# POST endpoints that only READ. They are safe to retry despite the verb.
_IDEMPOTENT_POSTS = ("/task/filter", "/task/completed", "/task/search")


def _seg(value: Any) -> str:
    """Quote one path segment. `safe=''` so an id containing '/' cannot change the endpoint."""
    return urllib.parse.quote(str(value), safe="")


def _http_error(exc: urllib.error.HTTPError, method: str, url: str) -> Exception:
    """Map an HTTPError onto our hierarchy, folding in the response body."""
    status = exc.code
    try:
        body = exc.read().decode("utf-8", "replace").strip()
    except Exception:  # a body we cannot read must not mask the status we can
        body = ""
    detail = f"{method} {url} -> HTTP {status}"
    if body:
        detail += f": {body[:_MAX_BODY_CHARS]}"

    throttled = _THROTTLE_MARKER in body or status == 429

    if throttled:
        # Deliberately not AuthError: the token is fine, we simply asked too often.
        err: Exception = ApiError(f"{detail} (rate limited — slow down)")
    elif status in (401, 403):
        err = AuthError(f"{detail} (token rejected — run `ticktick login`)")
    elif status == 404:
        err = NotFoundError(detail)
    else:
        err = ApiError(detail)
    err.status = status  # type: ignore[attr-defined]  # read by the retry loop and the CLI
    err.throttled = throttled  # type: ignore[attr-defined]
    return err


#: TickTick operates a second, separate service on dida365.com for mainland China —
#: different host, different accounts, same API shape. Rather than guess, the host is
#: overridable: `TICKTICK_API_BASE` in the environment, or `api_base` in the config.
DEFAULT_BASE = "https://api.ticktick.com/open/v1"
DIDA_BASE = "https://api.dida365.com/open/v1"


def resolve_base(cfg: dict | None = None) -> str:
    """An explicit API-root override, or '' when there is none.

    Empty rather than the default on purpose: callers pass the result straight to
    `Client(base=...)`, and a falsy value must leave `Client.BASE` exactly as it is.
    """
    env = os.environ.get("TICKTICK_API_BASE", "").strip()
    if env:
        return env.rstrip("/")
    configured = str((cfg or {}).get("api_base") or "").strip()
    return configured.rstrip("/")


class Client:
    """Authenticated TickTick Open API client. One instance per CLI invocation."""

    BASE = DEFAULT_BASE

    def __init__(self, token: str, *, timeout: float = 20.0, base: str | None = None) -> None:
        if not token:
            raise AuthError("no access token — run `ticktick login`")
        # An instance override beats the class attribute, which tests patch wholesale.
        if base:
            self.BASE = base.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    # ------------------------------------------------------------------ transport

    def request(self, method: str, path: str, body: dict | None = None) -> Any:
        """Perform one API call.

        Raises AuthError / NotFoundError / NetworkError / ApiError. Returns parsed JSON, or
        None when the response body is empty (complete/delete answer with 200 + nothing).
        GETs are retried on network failures and 5xx; mutations never are.
        """
        method = method.upper()
        url = f"{self.BASE}{path}"
        payload: bytes | None = None
        headers = dict(self._headers)
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        safe = method == "GET" or (method == "POST" and path in _IDEMPOTENT_POSTS)
        # One budget for every verb. A mutation is not retried on a plain failure —
        # the guard inside the loop enforces that — but it *is* retried when TickTick
        # says "too fast", because that answer means the request never reached the
        # user's data. Sizing the loop by `safe` instead would let a throttled
        # mutation sleep and then fall out of the loop having neither retried nor raised.
        attempts = max(len(_RETRY_DELAYS), len(_THROTTLE_DELAYS)) + 1
        for attempt in range(attempts):
            try:
                return self._send(method, url, payload, headers)
            except (NetworkError, ApiError) as exc:
                throttled = getattr(exc, "throttled", False)
                # Throttling is worth waiting out even on a mutation — the request never
                # reached the user's data, so replaying it cannot double-apply anything.
                retryable = (
                    throttled
                    or isinstance(exc, NetworkError)
                    or getattr(exc, "status", 0) >= 500
                )
                delays = _THROTTLE_DELAYS if throttled else _RETRY_DELAYS
                if attempt >= len(delays) or not (retryable and (safe or throttled)):
                    raise
                time.sleep(delays[attempt] + _JITTER_STEP * (attempt + 1))
        raise AssertionError("unreachable")  # loop either returns or raises

    def _send(
        self, method: str, url: str, payload: bytes | None, headers: dict[str, str]
    ) -> Any:
        req = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise _http_error(exc, method, url) from None
        except OSError as exc:
            # URLError, socket.timeout and bare socket errors all land here.
            raise NetworkError(f"{method} {url}: {exc}") from None

        if not raw.strip():
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError(f"{method} {url}: malformed JSON response ({exc})") from None

    # ------------------------------------------------------------------ projects

    def projects(self) -> list[dict]:
        """List the user's projects. Note the Inbox is NOT included by TickTick."""
        data = self.request("GET", "/project")
        return data if isinstance(data, list) else []

    def project(self, pid: str) -> dict:
        """Fetch one project."""
        return self.request("GET", f"/project/{_seg(pid)}") or {}

    def project_data(self, pid: str) -> dict:
        """Fetch ``{'project':…, 'tasks':[…], 'columns':[…]}``. Tasks are undone only."""
        return self.request("GET", f"/project/{_seg(pid)}/data") or {}

    def project_data_many(self, pids: list[str], *, workers: int = 8) -> dict[str, dict]:
        """Fetch several projects' data concurrently, preserving the input order.

        A project that fails (deleted, 403, flaky network) maps to ``{}`` so one bad project
        cannot blank the whole task list.
        """
        unique = list(dict.fromkeys(p for p in pids if p))
        if not unique:
            return {}
        results: dict[str, dict] = {}
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, min(workers, len(unique))),
            thread_name_prefix="ticktick-fanout",
        ) as pool:
            futures = {pool.submit(self.project_data, pid): pid for pid in unique}
            for future in concurrent.futures.as_completed(futures):
                pid = futures[future]
                try:
                    results[pid] = future.result() or {}
                except Exception:
                    results[pid] = {}
        return {pid: results.get(pid, {}) for pid in unique}

    def create_project(self, **fields: Any) -> dict:
        """Create a project (name, color, sortOrder, viewMode, kind)."""
        return self.request("POST", "/project", fields) or {}

    def update_project(self, pid: str, **fields: Any) -> dict:
        """Update a project; same body as create."""
        return self.request("POST", f"/project/{_seg(pid)}", fields) or {}

    def delete_project(self, pid: str) -> None:
        """Delete a project."""
        self.request("DELETE", f"/project/{_seg(pid)}")

    # ------------------------------------------------------------------ tasks

    def task(self, pid: str, tid: str) -> dict:
        """Fetch one task, including its checklist items."""
        return self.request("GET", f"/project/{_seg(pid)}/task/{_seg(tid)}") or {}

    def create_task(self, **fields: Any) -> dict:
        """Create a task. `title` is required; omitting `projectId` files it in the Inbox."""
        return self.request("POST", "/task", fields) or {}

    def update_task(self, tid: str, *, id: str, projectId: str, **fields: Any) -> dict:
        """Update a task. The API rejects the body unless it carries `id` and `projectId`,
        so both are re-asserted here and cannot be overridden by `fields`."""
        body = dict(fields)
        body["id"] = id
        body["projectId"] = projectId
        return self.request("POST", f"/task/{_seg(tid)}", body) or {}

    def complete_task(self, pid: str, tid: str) -> None:
        """Mark a task done."""
        # Empty JSON object rather than no body: a POST with no Content-Length upsets proxies.
        self.request("POST", f"/project/{_seg(pid)}/task/{_seg(tid)}/complete", {})

    def delete_task(self, pid: str, tid: str) -> None:
        """Delete a task."""
        self.request("DELETE", f"/project/{_seg(pid)}/task/{_seg(tid)}")

    def move_tasks(self, moves: list[dict]) -> list[dict]:
        """Move tasks between projects. Entries are {fromProjectId, toProjectId, taskId}.

        This is the only supported way to change a task's project — rewriting `projectId`
        through `update_task` is not a documented path and does not move the task.
        """
        if not moves:
            return []
        data = self.request("POST", "/task/move", moves)  # body is an array, even for one
        return data if isinstance(data, list) else []

    # -------------------------------------------------------------- bulk reads

    def filter_tasks(
        self,
        *,
        projectIds: list[str] | None = None,
        startDate: str | None = None,
        endDate: str | None = None,
        priority: list[int] | None = None,
        tag: list[str] | None = None,
        status: list[int] | None = None,
    ) -> list[dict]:
        """`POST /task/filter` — tasks matching the criteria, across every project.

        `filter_tasks(status=[0])` is every undone task the user has, Inbox included, in one
        request. That is the whole reason this client does not fan out per project.

        Note `startDate`/`endDate` filter on the task's **startDate**, not its dueDate, so a
        due-date view must fetch unfiltered and bucket client-side.
        """
        body: dict[str, Any] = {}
        if projectIds:
            body["projectIds"] = list(projectIds)
        if startDate:
            body["startDate"] = startDate
        if endDate:
            body["endDate"] = endDate
        if priority is not None:
            # The published parameter table spells this "proiority"; the doc's own example
            # uses "priority" and that is what the server accepts. Doc bug, not ours.
            body["priority"] = list(priority)
        if tag:
            body["tag"] = list(tag)
        if status is not None:
            body["status"] = list(status)
        data = self.request("POST", "/task/filter", body)
        return [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []

    def completed_tasks(
        self,
        *,
        projectIds: list[str] | None = None,
        startDate: str | None = None,
        endDate: str | None = None,
    ) -> list[dict]:
        """`POST /task/completed` — tasks whose `completedTime` falls in the range.

        The only way to see finished work: `/project/{id}/data` returns undone tasks only.
        """
        body: dict[str, Any] = {}
        if projectIds:
            body["projectIds"] = list(projectIds)
        if startDate:
            body["startDate"] = startDate
        if endDate:
            body["endDate"] = endDate
        data = self.request("POST", "/task/completed", body)
        return [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []

    def search_tasks(
        self,
        keywords: str,
        *,
        status: list[int] | None = None,
    ) -> list[dict]:
        """`POST /task/search` — full-text search across the **whole account**.

        Absent from the published documentation and worth far more than it looks: it
        reaches completed tasks and projects the undone list never fetched, so it is
        the only search that can answer "where did I put that" rather than merely
        filtering what is already on screen.

        Verified live: `keywords` and `status` are honoured; `projectIds`, `dueFrom`
        and `dueTo` are accepted and then **ignored** — the same query returned the
        same 150 rows with and without them, so nothing here may rely on them.
        Omitting `status` searches every state, `[0]` restricts it to undone.
        An empty `keywords` matches nothing rather than everything.
        """
        text = (keywords or "").strip()
        if not text:
            return []
        body: dict[str, Any] = {"keywords": text}
        if status is not None:
            body["status"] = list(status)
        data = self.request("POST", "/task/search", body)
        return [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []

    # ------------------------------------------------------------------ tags

    def tags(self) -> list[dict]:
        """`GET /tag` — every tag, as ``{name, label, sortOrder, color, parent, type}``.

        Undocumented but live-verified. `name` is the lowercase key the task's `tags`
        array holds; `label` is what the user typed and what should be displayed.
        `parent` names another tag's `name`, so tags form a tree.
        """
        data = self.request("GET", "/tag")
        return [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []

    def create_tag(self, name: str, label: str | None = None) -> dict:
        """`POST /tag` — create a tag. `label` defaults to `name`."""
        return self.request("POST", "/tag", {"name": name, "label": label or name}) or {}

    # -------------------------------------------------------- project groups

    def project_groups(self) -> list[dict]:
        """`GET /project/group` — the folders projects are filed under."""
        data = self.request("GET", "/project/group")
        return [g for g in data if isinstance(g, dict)] if isinstance(data, list) else []

    def create_project_group(self, name: str) -> dict:
        """`POST /project/group`."""
        return self.request("POST", "/project/group", {"name": name}) or {}

    def update_project_group(self, gid: str, **fields: Any) -> dict:
        """`POST /project/group/{id}`."""
        return self.request("POST", f"/project/group/{_seg(gid)}", fields) or {}

    def delete_project_group(self, gid: str) -> None:
        """`DELETE /project/group/{id}`."""
        self.request("DELETE", f"/project/group/{_seg(gid)}")

    def columns(self, pid: str) -> list[dict]:
        """`GET /project/{id}/column` — kanban columns, empty for a list project."""
        data = self.request("GET", f"/project/{_seg(pid)}/column")
        return [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []

    def inbox_data(self) -> dict:
        """`GET /project/inbox/data` — the Inbox, which `GET /project` never lists.

        The literal string "inbox" is accepted as a project id on this path only; the
        tasks it returns carry a real `projectId` of the form `inbox<userId>`.
        """
        return self.request("GET", "/project/inbox/data") or {}

    def all_undone(self) -> list[dict]:
        """Every undone task, by the cheapest route that works.

        One `/task/filter` call normally. If that endpoint fails for any reason other than a
        bad token, fall back to walking every project plus the Inbox — slower, and the path
        that can trip rate limiting, but it keeps the widget alive on an account or a
        deployment where `/task/filter` misbehaves.
        """
        try:
            return self.filter_tasks(status=[0])
        except AuthError:
            raise  # a bad token will not be fixed by asking a different way
        except (ApiError, NetworkError):
            pass

        tasks: list[dict] = []
        seen: set[str] = set()
        pids = [p["id"] for p in self.projects() if isinstance(p, dict) and p.get("id")]
        blobs = list(self.project_data_many(pids).values())
        try:
            blobs.append(self.inbox_data())
        except (ApiError, NetworkError, NotFoundError):
            pass  # no Inbox is better than no tasks
        for blob in blobs:
            for task in (blob or {}).get("tasks") or []:
                tid = isinstance(task, dict) and str(task.get("id") or "")
                if tid and tid not in seen:
                    seen.add(tid)
                    tasks.append(task)
        return tasks


def client_from_config(cfg: dict | None = None) -> Client:
    """Build a Client from the stored credentials.

    Raises:
        AuthError: no token is stored.
    """
    cfg = _config.load() if cfg is None else cfg
    return Client(_config.token(cfg), base=resolve_base(cfg))


__all__ = ["Client", "client_from_config", "USER_AGENT"]
