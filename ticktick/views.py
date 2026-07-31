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

#: TickTick's smart lists, plus `project` for one list of the user's own.
VIEWS = ("today", "tomorrow", "next", "overdue", "inbox", "all", "project")

#: Buckets each view admits; None means "every bucket" (undated is gated separately).
#: These are TickTick's own rules: Today is what is due today *plus* what is late,
#: because a task that slipped is still today's problem. Tomorrow is tomorrow alone.
#: A list — the Inbox or one of the user's — is not a date filter at all.
_VIEW_BUCKETS: dict[str, frozenset[str] | None] = {
    "today": frozenset({"overdue", "today"}),
    "tomorrow": frozenset({"tomorrow"}),
    "next": frozenset({"overdue", "today", "tomorrow", "upcoming"}),
    "overdue": frozenset({"overdue"}),
    "inbox": None,
    "all": None,
    "project": None,
}

#: Views that are a list rather than a date window, and so show undated tasks and
#: are counted per project rather than per bucket.
_LIST_VIEWS = frozenset({"inbox", "project"})

#: How the rows are ordered, and therefore how the UI sections them. `list` is
#: TickTick's default for a smart list: group under the list each task belongs to.
SORTS = ("list", "time", "priority", "title")

_COUNT_KEYS = ("overdue", "today", "tomorrow", "upcoming", "later", "undated")

#: A TickTick task is either something to do or something to read. Notes carry
#: `kind: "NOTE"`; everything else — "TEXT", "CHECKLIST", or a field the API simply
#: omitted — is a to-do. Notes have no checkbox and no completion, so counting them
#: in a badge would report work that does not exist.
NOTE_KIND = "NOTE"

INBOX_NAME = "Inbox"

#: The Inbox's project id reads `inbox<userId>`; every other project id is a hex ObjectId,
#: so no real project can collide with this prefix.
_INBOX_PREFIX = "inbox"

#: Stand-in for a task whose project is not in the catalogue. Never mutated.
_NO_PROJECT: dict[str, str] = {"id": "", "name": "", "color": "", "groupId": ""}


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
    sort: str = "list",
) -> dict:
    """Build the widget's payload for one view.

    Returns ``{'tasks': [row, …], 'projects': [{'id','name','color','count','groupId'}, …],
    'groups': [{'id','name'}, …], 'counts': {…}, 'sort': str, 'cache_dirty': bool}``.

    `counts` is computed over every task in the account *before* view/search/priority/
    limit filtering, so the bar badge neither flickers when the user types in the search
    box nor changes meaning when they browse into a single list. Notes are excluded from
    every count: a note has no checkbox, so counting one reports work that does not exist.
    `cache_dirty` is True when `cfg` learned the Inbox id; `cfg` is mutated in place and
    the caller decides whether to `config.save` it.

    Note this no longer records a ``taskId -> projectId`` entry per task. The local task
    cache already holds every task with its project, so the copy in `cfg` was redundant —
    and on an account with more than `PROJECT_CACHE_MAX` tasks it thrashed, evicting and
    re-adding on every single read and rewriting the file that holds the user's token
    each time.

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
    sort = str(sort or "list").strip().lower()
    if sort not in SORTS:
        raise UsageError(f"unknown sort {sort!r}; expected one of {', '.join(SORTS)}")

    now = _dates.now()
    known = catalogue(client, cfg)
    # `known` entries and `by_id` values are the same objects, so filling a name in
    # either place fills it in both.
    by_id = {p["id"]: p for p in known}
    dirty = False

    # One request for the whole account, whatever the view. Fanning out per project is
    # what trips the rate limiter, `/task/filter` already covers the Inbox, and scoping
    # the *fetch* to one list is what used to make the bar badge change meaning when the
    # user merely browsed into that list.
    fetched = client.all_undone()

    inbox = _inbox_id(fetched) or _text(cfg.get("inbox_id"))
    if inbox and inbox != _text(cfg.get("inbox_id")):
        # Learned for free off any Inbox task, which is why nothing probes for it.
        cfg["inbox_id"] = inbox
        dirty = True
    if inbox and inbox not in by_id:
        entry = {"id": inbox, "name": INBOX_NAME, "color": "", "groupId": ""}
        known.insert(0, entry)  # the Inbox reads first in the UI, as in TickTick itself
        by_id[inbox] = entry

    # The Inbox is a list like any other once its id is known; asking for it by name
    # rather than by id is the whole point of it being a smart list. `scoped` is kept
    # separate from `scope` because an empty scope has to mean "nothing matches", not
    # "no filter": an Inbox with nothing in it yields no id to match on, and treating
    # that as unfiltered showed the user's entire account under the heading "Inbox".
    scoped = view in _LIST_VIEWS
    scope = inbox if view == "inbox" else ""
    if view == "project":
        scope = project
        if project not in by_id:
            # Not in the open catalogue: closed, archived, shared, or an id the user
            # typed by hand. `/task/filter` does not return its tasks, so this is the
            # one case that still needs the per-project endpoint — which is also the
            # only place a closed project's real name and colour survive.
            meta = {"id": project, "name": "", "color": "", "groupId": ""}
            known.append(meta)
            by_id[project] = meta
            fetched = fetched + _project_tasks(client, cfg, project, meta)

    # Every task carries its own `projectId`; a project view can still answer for one
    # that somehow does not, because it asked for a specific project.
    fallback = project if view == "project" else ""
    rows: list[dict] = []
    per_project: dict[str, int] = {}
    seen: set[str] = set()

    for task in fetched:
        if not isinstance(task, dict):
            continue
        # A closed project's tasks are appended to the account-wide list, and the two
        # can overlap; the same task twice would double every count it lands in.
        tid = _text(task.get("id"))
        if tid and tid in seen:
            continue
        if tid:
            seen.add(tid)
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
        if not item["isNote"]:
            per_project[pid] = per_project.get(pid, 0) + 1

    counts = {key: 0 for key in _COUNT_KEYS}
    notes = 0
    for item in rows:
        if item["isNote"]:
            notes += 1
            continue
        counts[item["bucket"]] += 1
    counts["notes"] = notes
    counts["total"] = len(rows) - notes

    keep = _VIEW_BUCKETS[view]
    # A list view that hid its undated tasks would look empty for anyone who files by
    # list instead of by date — which is most people's Inbox.
    undated_ok = include_undated or view in _LIST_VIEWS
    needle = search.strip().lower()
    # Any-of, not all-of: `--tag work --tag home` is "either", which is how every
    # tag filter people already use behaves.
    wanted_tags = {str(t).strip().lstrip("#").lower() for t in tags or [] if str(t).strip()}

    visible = [
        item
        for item in rows
        if (not scoped or item["projectId"] == scope)
        and (keep is None or item["bucket"] in keep)
        and (undated_ok or item["bucket"] != "undated")
        and (priority is None or item["priority"] == priority)
        and (not wanted_tags or wanted_tags & {t.lower() for t in item["tags"]})
        and (not needle or needle in item["title"].lower() or needle in item["content"].lower())
    ]
    visible.sort(key=_sorter(sort, {p["id"]: i for i, p in enumerate(known)}))
    if limit is not None and limit > 0:
        visible = visible[:limit]

    return {
        "tasks": visible,
        "projects": [
            {
                "id": p["id"],
                "name": p["name"],
                "color": p["color"],
                "groupId": p.get("groupId", ""),
                "count": per_project.get(p["id"], 0),
            }
            for p in known
        ],
        "groups": groups(client),
        "counts": counts,
        "sort": sort,
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
    # Absent on most payloads; TickTick only spells it out for the kinds that are not
    # a plain to-do, so "missing" has to read as TEXT rather than as unknown.
    kind = _text(task.get("kind")).upper() or "TEXT"
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
        # The unambiguous spelling, for an editor to seed a field with. `startLabel`
        # is written to be read ("Mon"), and re-parsing that would move the date.
        "startIso": start.isoformat() if start else "",
        # Real nested subtasks: `parentId` points at the owning task.
        "parentId": _text(task.get("parentId")),
        "sortOrder": _int(task.get("sortOrder")),
        "status": _int(task.get("status")),
        "progress": _int(task.get("progress")),
        # True while a locally-applied change has not reached TickTick yet.
        "pending": bool(task.get("_pending")),
        # "TEXT" | "CHECKLIST" | "NOTE". A note is something to read, not something to
        # do: it has no checkbox in TickTick and never counts toward anything.
        "kind": kind,
        "isNote": kind == NOTE_KIND,
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
                    # Which folder the list sits in, straight off `/project`. Free —
                    # the field is already in the response the catalogue reads.
                    "groupId": _text(entry.get("groupId")),
                }
            )
    inbox = _text(cfg.get("inbox_id"))
    if inbox and not any(p["id"] == inbox for p in out):
        out.insert(0, {"id": inbox, "name": INBOX_NAME, "color": "", "groupId": ""})
    return out


def groups(client: Client) -> list[dict]:
    """The folders lists are filed under, as ``[{'id','name','sortOrder'}, …]``.

    Folders are decoration: they change how the list of lists reads, never which tasks
    exist. So anything the endpoint does — 404 because the account has none, or an
    error because it is undocumented and may change — degrades to a flat list rather
    than failing a read the user asked for.
    """
    getter = getattr(client, "project_groups", None)
    if getter is None:
        return []  # a source that predates folders, or a stub in a test
    try:
        raw = getter()
    except Exception:
        return []
    out = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        gid = _text(entry.get("id"))
        if gid:
            out.append(
                {
                    "id": gid,
                    "name": _text(entry.get("name")) or gid,
                    "sortOrder": _int(entry.get("sortOrder")),
                }
            )
    out.sort(key=lambda g: (g["sortOrder"], g["name"].lower()))
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


def _sorter(sort: str, rank: dict[str, int]):
    """The row order for one sort mode — TickTick's four list options.

    The UI sections on whatever the leading key is, so this decides the headings as
    well as the order: sort by list and the headings are list names, sort by time and
    they are Overdue/Today/Tomorrow. `rank` is the catalogue's own order, so the lists
    read down the popup in the order they read down TickTick's sidebar, Inbox first.
    A list not in the catalogue sorts last rather than first, which is what an
    unranked `0` would have done.
    """
    if sort == "list":
        return lambda i: (
            rank.get(i["projectId"], len(rank) + 1),
            # The id itself, because every list the catalogue does not know collapses
            # to the same rank. Without it their rows interleave, and the UI — which
            # opens a heading wherever the id changes — heads the same list twice.
            i["projectId"],
            _dates.BUCKET_ORDER.get(i["bucket"], 9),
            i["due"] or float("inf"),
            -i["priority"],
            i["title"].lower(),
        )
    if sort == "priority":
        return lambda i: (
            -i["priority"],
            _dates.BUCKET_ORDER.get(i["bucket"], 9),
            i["due"] or float("inf"),
            i["title"].lower(),
        )
    if sort == "title":
        return lambda i: (i["title"].lower(), i["due"] or float("inf"))
    return _sort_key


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


__all__ = ["VIEWS", "SORTS", "INBOX_NAME", "NOTE_KIND", "collect", "row", "catalogue", "groups"]
