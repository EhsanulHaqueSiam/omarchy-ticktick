"""The task list the bar widget renders: fetch, bucket, filter, sort, flatten.

A full view is two requests: `/project` for names and colours, and one `/task/filter`
via :meth:`ticktick.api.Client.all_undone` for every undone task in the account, Inbox
included. The per-project fan-out it replaced is what trips TickTick's rate limiter, so
nothing here may reintroduce it. A single-project view still fetches just that project —
through `/project/inbox/data` when the target is the Inbox, which is not addressable
through `/project/{id}/data` with its real id.

Two consequences the UI inherits:

* only **undone** tasks come back, completed ones need `/task/completed`, and
* `/task/filter` answers with a flat task list carrying no project metadata, so each
  task is joined to its project by `projectId`. A task whose project is unknown —
  closed, shared, or created since `/project` was read — renders with a blank project
  name; dropping the row would silently lose work.

The Inbox is never in `/project`, so its catalogue entry is synthesised. Its id is
stamped on every Inbox task as `inbox<userId>`, which is where this reads it from;
`cfg['inbox_id']` is only a cache of that, never a prerequisite.

Rows are emitted with every documented key always present and never null. QML binds
straight to these; a missing key renders as the literal string `undefined` on screen,
which is worse than an empty one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import config as _config
from . import dates as _dates
from .api import Client
from .errors import UsageError

VIEWS = ("today", "overdue", "next", "all", "project")

#: Buckets each view admits; None means "every bucket" (undated is gated separately).
_VIEW_BUCKETS: dict[str, frozenset[str] | None] = {
    "today": frozenset({"overdue", "today"}),
    "overdue": frozenset({"overdue"}),
    "next": frozenset({"overdue", "today", "tomorrow", "upcoming"}),
    "all": None,
    "project": None,
}

_COUNT_KEYS = ("overdue", "today", "tomorrow", "upcoming", "later", "undated")

INBOX_NAME = "Inbox"

#: The Inbox's project id reads `inbox<userId>`; every other project id is a hex ObjectId,
#: so no real project can collide with this prefix.
_INBOX_PREFIX = "inbox"

#: Stand-in for a task whose project is not in the catalogue. Never mutated.
_NO_PROJECT: dict[str, str] = {"id": "", "name": "", "color": ""}


def collect(
    client: Client,
    cfg: dict,
    *,
    view: str = "today",
    project: str | None = None,
    upcoming_days: int = 7,
    search: str = "",
    priority: int | None = None,
    tags: list[str] | None = None,
    include_undated: bool = False,
    limit: int | None = None,
) -> dict:
    """Build the widget's payload for one view.

    Returns ``{'tasks': [row, …], 'projects': [{'id','name','color','count'}, …],
    'counts': {…}, 'cache_dirty': bool}``.

    `counts` is computed over every fetched task *before* view/search/priority/limit
    filtering, so the bar badge does not flicker when the user types in the search box.
    Every project's `count` is exact for the whole-account views, because they all come
    out of one flat task list; under `view='project'` only the fetched project has one.
    `cache_dirty` is True when `cfg` gained a ``taskId -> projectId`` entry or learned the
    Inbox id; `cfg` is mutated in place and the caller decides whether to `config.save` it.

    Raises:
        UsageError: unknown view, or `view='project'` without a project id.
        AuthError / NetworkError / ApiError: propagated from the client.
    """
    if view not in VIEWS:
        raise UsageError(f"unknown view {view!r}; expected one of {', '.join(VIEWS)}")
    project = str(project or "").strip()
    if view == "project" and not project:
        raise UsageError("view 'project' requires a project id")
    upcoming_days = max(0, int(upcoming_days))

    now = _dates.now()
    known = catalogue(client, cfg)
    # `known` entries and `by_id` values are the same objects, so filling a name in
    # either place fills it in both.
    by_id = {p["id"]: p for p in known}
    dirty = False

    if view == "project":
        meta = by_id.get(project)
        if meta is None:
            # A project id the user typed by hand, or one that is closed/archived: still
            # honour it rather than returning an empty list with no explanation.
            meta = {"id": project, "name": "", "color": ""}
            known.append(meta)
            by_id[project] = meta
        fetched = _project_tasks(client, cfg, project, meta)
    else:
        # One request for the whole account. Fanning out per project is what trips the
        # rate limiter, and `/task/filter` already covers the Inbox.
        fetched = client.all_undone()

    inbox = _inbox_id(fetched) or _text(cfg.get("inbox_id"))
    if inbox and inbox != _text(cfg.get("inbox_id")):
        # Learned for free off any Inbox task, which is why nothing probes for it.
        cfg["inbox_id"] = inbox
        dirty = True
    if inbox and inbox not in by_id:
        entry = {"id": inbox, "name": INBOX_NAME, "color": ""}
        known.insert(0, entry)  # the Inbox reads first in the UI, as in TickTick itself
        by_id[inbox] = entry

    # Every task carries its own `projectId`; a project view can still answer for one
    # that somehow does not, because it asked for a specific project.
    fallback = project if view == "project" else ""
    rows: list[dict] = []
    per_project: dict[str, int] = {}

    for task in fetched:
        if not isinstance(task, dict):
            continue
        pid = _text(task.get("projectId")) or fallback
        meta = by_id.get(pid) or _NO_PROJECT
        item = row(
            task,
            project_name=meta["name"],
            project_color=meta["color"],
            now=now,
            upcoming_days=upcoming_days,
            fallback_project_id=fallback,
        )
        rows.append(item)
        per_project[pid] = per_project.get(pid, 0) + 1
        # Free knowledge: every refresh teaches us where tasks live, which is what
        # lets `ticktick complete <id>` skip its own N+1 scan later.
        if item["id"] and _config.cached_project(cfg, item["id"]) != item["projectId"]:
            _config.cache_project(cfg, item["id"], item["projectId"])
            dirty = True

    counts = {key: 0 for key in _COUNT_KEYS}
    for item in rows:
        counts[item["bucket"]] += 1
    counts["total"] = len(rows)

    keep = _VIEW_BUCKETS[view]
    # A project view that hid its undated tasks would look empty for anyone who files
    # by project instead of by date.
    undated_ok = include_undated or view == "project"
    needle = search.strip().lower()
    # Any-of, not all-of: `--tag work --tag home` is "either", which is how every
    # tag filter people already use behaves.
    wanted_tags = {str(t).strip().lstrip("#").lower() for t in tags or [] if str(t).strip()}

    visible = [
        item
        for item in rows
        if (keep is None or item["bucket"] in keep)
        and (undated_ok or item["bucket"] != "undated")
        and (priority is None or item["priority"] == priority)
        and (not wanted_tags or wanted_tags & {t.lower() for t in item["tags"]})
        and (not needle or needle in item["title"].lower() or needle in item["content"].lower())
    ]
    visible.sort(key=_sort_key)
    if limit is not None and limit > 0:
        visible = visible[:limit]

    return {
        "tasks": visible,
        "projects": [
            {
                "id": p["id"],
                "name": p["name"],
                "color": p["color"],
                "count": per_project.get(p["id"], 0),
            }
            for p in known
        ],
        "counts": counts,
        "cache_dirty": dirty,
    }


def row(
    task: dict,
    *,
    project_name: str = "",
    project_color: str = "",
    now: datetime | None = None,
    upcoming_days: int = 7,
    fallback_project_id: str = "",
) -> dict:
    """Flatten one API task into the row shape QML binds to.

    Every key is present and non-null: strings default to ``''``, numbers to ``0``,
    lists to ``[]``. Never raises on a malformed task.
    """
    now = now or _dates.now()
    all_day = bool(task.get("isAllDay"))
    due = _dates.parse(
        task.get("dueDate"), all_day=all_day, tz_name=task.get("timeZone")
    )
    items = _items(task.get("items"))
    done = sum(1 for entry in items if entry["done"])
    start = _dates.parse(task.get("startDate"), all_day=all_day, tz_name=task.get("timeZone"))
    return {
        "id": _text(task.get("id")),
        "projectId": _text(task.get("projectId")) or fallback_project_id,
        "project": project_name,
        "projectColor": project_color,
        "title": _text(task.get("title")),
        # TickTick writes the note to `content`; `desc` is the checklist's blurb.
        "content": _text(task.get("content")) or _text(task.get("desc")),
        "bucket": _dates.bucket(
            due, all_day=all_day, now=now, upcoming_days=upcoming_days
        ),
        "due": int(due.timestamp()) if due else 0,
        "dueLabel": _dates.label(due, all_day=all_day, now=now),
        "dueIso": due.isoformat() if due else "",
        "priority": _int(task.get("priority")),
        "isAllDay": all_day,
        "repeat": _text(task.get("repeatFlag")),
        "reminders": _reminders(task.get("reminders")),
        "items": items,
        "itemsDone": done,
        "itemsTotal": len(items),
        # Fields the live API returns that the published documentation omits. They
        # cost nothing to carry and the UI cannot show what it never receives.
        "tags": _tags(task.get("tags")),
        # A TickTick task has a start as well as a due; a range reads "Mon → Fri".
        "start": int(start.timestamp()) if start else 0,
        "startLabel": _dates.label(start, all_day=all_day, now=now) if start else "",
        # Real nested subtasks: `parentId` points at the owning task.
        "parentId": _text(task.get("parentId")),
        "sortOrder": _int(task.get("sortOrder")),
        "status": _int(task.get("status")),
        "progress": _int(task.get("progress")),
        # True while a locally-applied change has not reached TickTick yet.
        "pending": bool(task.get("_pending")),
    }


def catalogue(client: Client, cfg: dict) -> list[dict]:
    """Open projects, with the remembered Inbox in front of them.

    TickTick omits the Inbox from `/project` entirely, and closed projects are archive
    noise nobody wants counted in a badge. The Inbox entry is synthesised from the
    cached `cfg['inbox_id']`; `collect` re-derives that id from the tasks themselves, so
    an empty cache costs one refresh, not a missing Inbox.
    """
    raw = client.projects()
    out: list[dict] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or entry.get("closed"):
            continue
        pid = _text(entry.get("id"))
        if pid:
            out.append(
                {
                    "id": pid,
                    "name": _text(entry.get("name")) or pid,
                    "color": _text(entry.get("color")),
                }
            )
    inbox = _text(cfg.get("inbox_id"))
    if inbox and not any(p["id"] == inbox for p in out):
        out.insert(0, {"id": inbox, "name": INBOX_NAME, "color": ""})
    return out


# --- internals ---------------------------------------------------------------


def _project_tasks(client: Client, cfg: dict, pid: str, meta: dict) -> list[dict]:
    """Undone tasks for one project, filling `meta` in place from what `/data` echoes.

    The Inbox has to go through `/project/inbox/data`: it is not reachable through
    `/project/{id}/data` under its real `inbox<userId>` id. That path echoes no project
    object, so the Inbox's name is the synthesised one either way.
    """
    if pid.startswith(_INBOX_PREFIX) or pid == _text(cfg.get("inbox_id")):
        blob = client.inbox_data()
        meta["name"] = meta["name"] or INBOX_NAME
    else:
        blob = client.project_data(pid)
    if not isinstance(blob, dict):
        return []
    # The echoed project object is the only metadata this path gets, and the only place
    # a closed project's real name and colour still appear.
    echoed = blob.get("project")
    if isinstance(echoed, dict):
        meta["name"] = _text(echoed.get("name")) or meta["name"]
        meta["color"] = _text(echoed.get("color")) or meta["color"]
    tasks = blob.get("tasks")
    return tasks if isinstance(tasks, list) else []


def _inbox_id(tasks: list) -> str:
    """The Inbox's real project id, read off the first task that lives in it."""
    for task in tasks:
        pid = _text(task.get("projectId")) if isinstance(task, dict) else ""
        if pid.startswith(_INBOX_PREFIX):
            return pid
    return ""


def _sort_key(item: dict) -> tuple[int, float, int, str]:
    """Group order, then due ascending, then priority descending, then title."""
    return (
        _dates.BUCKET_ORDER.get(item["bucket"], 9),
        item["due"] or float("inf"),  # undated sinks to the bottom of its bucket
        -item["priority"],
        item["title"].lower(),
    )


def _items(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        out.append(
            {
                "id": _text(entry.get("id")),
                "title": _text(entry.get("title")),
                "done": _int(entry.get("status")) > 0,
            }
        )
    return out


def _tags(raw: Any) -> list[str]:
    """A task's tag names, or [] when it has none.

    The API omits empty fields rather than sending nulls, so `tags` is simply absent
    from a task that carries none — which is why this must not distinguish "missing"
    from "empty". Observed live: `tags` appears on tasks that have them and is gone
    from those that do not, on every endpoint that returns tasks.
    """
    if not isinstance(raw, list):
        return []
    return [_text(tag) for tag in raw if _text(tag)]


def _reminders(raw: Any) -> list[str]:
    """Reminders come back as trigger strings, but objects have been seen too."""
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        if isinstance(entry, dict):
            entry = entry.get("trigger") or entry.get("id")
        text = _text(entry)
        if text:
            out.append(text)
    return out


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = ["VIEWS", "INBOX_NAME", "collect", "row", "catalogue"]
