"""Command line front end. The only module in this package that writes to stdout.

The contract QML depends on: **exactly one JSON object on stdout and exit status 0**,
for every subcommand except `auth` and `selftest`, which are human-facing. Failures are
data, not exceptions — a dead network prints ``{"ok": false, "error": "network", …}`` so
the widget can render "offline" instead of a stack trace.

That is also why argparse is subclassed: its default behaviour on a bad flag is to print
usage to stderr and exit 2, which would leave the caller parsing an empty stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
from datetime import datetime, time as _time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import __version__, api, auth, config, dates, nlp, render, store, views
from .errors import (
    ApiError,
    AuthError,
    NetworkError,
    NotFoundError,
    TickTickError,
    UsageError,
)

#: Subcommands that talk to a person, may print prose, and may exit non-zero.
HUMAN_COMMANDS = frozenset({"auth", "selftest"})

_PRIORITY_WORDS = {
    "none": 0, "0": 0, "p0": 0,
    "low": 1, "l": 1, "1": 1, "p3": 1,
    "medium": 3, "med": 3, "m": 3, "3": 3, "p2": 3,
    "high": 5, "h": 5, "5": 5, "p1": 5,
}

#: The spec's CLI spells the upcoming view `next7`; the frozen `views.VIEWS` spells it
#: `next`. Accept both so neither document is a lie.
_VIEW_ALIASES = {"next7": "next"}

#: A TickTick timestamp ends in a four-digit UTC offset. Anything date-shaped that does
#: not is refused before it reaches the wire — see `_wire_date`.
_OFFSET_RE = re.compile(r"[+-]\d{4}$")

#: `9am` / `12pm`: a wall-clock reminder, which only means something on an all-day task.
_CLOCK_RE = re.compile(r"^(\d{1,2})(am|pm)$")


# --- output ------------------------------------------------------------------


#: Set by `main` once argv is known, so `_emit` can render the right shape.
_OUTPUT: dict[str, Any] = {"command": "", "human": False}


def _emit(payload: dict) -> int:
    """Print the one object this invocation is allowed to print.

    JSON to a pipe, formatted text to a terminal. The widget always redirects stdout,
    so it always gets JSON — there is no flag for it to forget and no way for a new
    call site to accidentally receive prose.
    """
    if _OUTPUT["human"]:
        sys.stdout.write(render.render(str(_OUTPUT["command"]), payload) + "\n")
    else:
        json.dump(payload, sys.stdout, ensure_ascii=False, default=str)
        sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


def _fail(exc: BaseException) -> int:
    kind = getattr(exc, "kind", None)
    message = str(exc) if kind else repr(exc)
    return _emit({"ok": False, "error": kind or "api", "message": message})


# --- argument parsing --------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """argparse that raises instead of killing the process on a bad argument."""

    def error(self, message: str) -> Any:  # noqa: D102 - argparse's own contract
        raise UsageError(f"{self.prog}: {message}")

    def exit(self, status: int = 0, message: str | None = None) -> Any:  # noqa: D102
        if status == 0:
            raise SystemExit(0)  # --help / --version already printed themselves
        raise UsageError((message or "").strip() or f"{self.prog}: invalid arguments")


def _priority(value: str) -> int:
    """Accept 0/1/3/5 or high/medium/low/none. TickTick has no other levels."""
    try:
        return _PRIORITY_WORDS[value.strip().lower()]
    except KeyError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a priority — use none/low/medium/high or 0/1/3/5"
        ) from None


def _output_flags() -> argparse.ArgumentParser:
    """`--json` / `--text`, shared by every subcommand through argparse `parents`.

    They have to be real options on every parser rather than something filtered out of
    argv by hand: a task really can be titled ``--json``, and stripping the token
    wherever it appears turns `ticktick add -- --json` into a usage error. Declared
    properly, argparse applies the ordinary `--` convention and the title survives.

    `SUPPRESS` keeps the subcommand copy from overwriting a value the top-level parser
    already set, which is what would otherwise make `ticktick --json tasks` silently
    fall back to text.
    """
    shared = argparse.ArgumentParser(add_help=False)
    # Explicit dests: `--text` would otherwise land on `args.text`, which is already
    # the positional holding the task title in `add`, `parse` and `search`. That
    # collision made every one of those commands print prose no matter what.
    shared.add_argument("--json", action="store_true", dest="force_json",
                        default=argparse.SUPPRESS,
                        help="force JSON output even on a terminal")
    shared.add_argument("--text", action="store_true", dest="force_text",
                        default=argparse.SUPPRESS,
                        help="force human-readable output even through a pipe")
    return shared


def _build_parser() -> _Parser:
    shared = _output_flags()
    parser = _Parser(prog="ticktick", description="TickTick from the Omarchy bar.",
                     parents=[shared])
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    def cmd(name: str, help_text: str, handler: Callable[..., Any]) -> _Parser:
        child = sub.add_parser(name, help=help_text, description=help_text,
                               parents=[shared])
        child.set_defaults(func=handler)
        return child

    def with_project(child: _Parser) -> _Parser:
        child.add_argument(
            "--project",
            metavar="ID",
            default=None,
            help="project id; resolved from the cache or by scanning when omitted",
        )
        return child

    def with_offline(child: _Parser) -> _Parser:
        """Every mutation can be made without a network; this forces that path."""
        child.add_argument(
            "--offline",
            action="store_true",
            help="apply locally and queue for later instead of contacting TickTick",
        )
        return child

    p = cmd("auth", "Sign in to TickTick via OAuth2.", _cmd_auth)
    p.add_argument("--redirect", default=None, metavar="URL",
                   help=f"redirect uri registered on your app (default {auth.DEFAULT_REDIRECT})")
    p.add_argument("--client-id", default=None, metavar="ID")
    p.add_argument("--client-secret", default=None, metavar="SECRET")
    p.add_argument("--no-browser", action="store_true",
                   help="print the authorization URL instead of opening a browser")

    p = cmd(
        "login",
        "Sign in with a TickTick API token (Settings -> Account -> API Token). "
        "Reads the token from stdin so it never appears in `ps` or your shell history.",
        _cmd_login,
    )
    p.add_argument("--token", default=None, metavar="TOKEN",
                   help="pass the token directly instead of on stdin; note this makes "
                        "it visible to `ps` for every user on the machine")
    p.add_argument("--api-base", default=None, metavar="URL",
                   help=f"API root to use from now on, for dida365.com accounts "
                        f"(default {api.DEFAULT_BASE}, dida365 is {api.DIDA_BASE})")

    cmd("logout", "Forget the stored token.", _cmd_logout)

    cmd("status", "Report sign-in state, token expiry, inbox id and counts.", _cmd_status)
    with_offline(
        cmd("projects", "List projects (including the Inbox) with open task counts.", _cmd_projects)
    )
    cmd("tags", "List the account's tags.", _cmd_tags)

    p = cmd("search", "Search every task in the account, completed ones included.", _cmd_search)
    p.add_argument("text", nargs="+")
    p.add_argument("--all-states", action="store_true",
                   help="include completed and abandoned tasks (undone only by default)")
    p.add_argument("--limit", type=int, default=None, metavar="N")

    p = cmd("tasks", "List tasks for a view.", _cmd_tasks)
    p.add_argument("--view", default="today",
                   choices=sorted(set(views.VIEWS) | set(_VIEW_ALIASES)))
    p.add_argument("--project", default=None, metavar="ID")
    p.add_argument("--days", type=int, default=7, metavar="N",
                   help="how far the 'upcoming' bucket reaches (default 7)")
    p.add_argument("--search", default="", metavar="TEXT")
    p.add_argument("--priority", type=_priority, default=None, metavar="P")
    p.add_argument("--tag", action="append", default=None, metavar="TAG",
                   help="only tasks carrying this tag; repeatable (any-of)")
    p.add_argument("--include-undated", action="store_true")
    p.add_argument("--limit", type=int, default=None, metavar="N")
    with_offline(p)
    p.add_argument("--refresh", action="store_true",
                   help="always go to TickTick, however fresh the cache is")
    p.add_argument("--max-age", type=float, default=None, metavar="SECONDS",
                   help=f"serve the cache without refetching for this long "
                        f"(default {store.DEFAULT_MAX_AGE:g})")

    with_offline(cmd("sync", "Flush queued changes and refresh the cache.", _cmd_sync))

    p = with_project(cmd("task", "Show one task with its checklist.", _cmd_task))
    p.add_argument("taskId")

    p = with_offline(with_project(cmd("add", "Create a task from natural language.", _cmd_add)))
    p.add_argument("text", nargs="+", help='e.g. "submit report tomorrow 5pm !high #work"')
    p.add_argument("--tag", action="append", default=None, metavar="TAG",
                   help="attach a tag; repeatable")
    p.add_argument("--parent", default=None, metavar="TASK_ID",
                   help="create it as a subtask of this task")
    p.add_argument("--start", default=None, metavar="WHEN",
                   help="start date, for a task that spans a range")
    p.add_argument("--due", default=None, metavar="WHEN", help="natural language or a date")
    p.add_argument("--priority", type=_priority, default=None, metavar="P")
    p.add_argument("--content", default=None, metavar="TEXT")
    p.add_argument("--all-day", action="store_true")
    p.add_argument("--remind", action="append", default=None, metavar="SPEC",
                   help="'on time', '15m', '1h', '1d', '9am' (all-day only), or a raw "
                        "TRIGGER: value; needs a due date or TickTick drops it")
    p.add_argument("--repeat", default=None, metavar="RRULE",
                   help="'daily', 'every 3 weeks', or a raw RRULE: value")

    p = with_offline(with_project(cmd("edit", "Change fields on an existing task.", _cmd_edit)))
    p.add_argument("taskId")
    p.add_argument("--title", default=None)
    p.add_argument("--due", default=None, metavar="WHEN")
    p.add_argument("--clear-due", action="store_true")
    p.add_argument("--priority", type=_priority, default=None, metavar="P")
    p.add_argument("--content", default=None, metavar="TEXT")
    p.add_argument("--move-to", default=None, metavar="PROJECT_ID",
                   help="move the task to this project (same endpoint as `ticktick move`)")
    p.add_argument("--start", default=None, metavar="WHEN",
                   help="start date; a task can span a range, not just a deadline")
    p.add_argument("--tag", action="append", default=None, metavar="TAG",
                   help="replace the task's tags; repeatable, empty clears them")
    p.add_argument("--add-tag", action="append", default=None, metavar="TAG")
    p.add_argument("--remove-tag", action="append", default=None, metavar="TAG")
    p.add_argument("--parent", default=None, metavar="TASK_ID",
                   help="make this a subtask of another task ('' detaches it)")

    p = with_project(cmd("move", "Move a task to another project.", _cmd_move))
    p.add_argument("taskId")
    p.add_argument("toProjectId")

    p = cmd(
        "completed",
        "List recently completed tasks. The window filters on completedTime, not on the "
        "due date, and completing is one-way: the Open API cannot un-complete a task.",
        _cmd_completed,
    )
    p.add_argument("--days", type=int, default=7, metavar="N",
                   help="how far back to look (default 7)")
    p.add_argument("--project", default=None, metavar="ID",
                   help="restrict to one project id")
    p.add_argument("--limit", type=int, default=None, metavar="N")

    p = with_offline(with_project(cmd("complete", "Mark a task done.", _cmd_complete)))
    p.add_argument("taskId")

    p = with_offline(with_project(cmd("delete", "Delete a task permanently.", _cmd_delete)))
    p.add_argument("taskId")
    p.add_argument("--yes", action="store_true", help="accepted for symmetry; deletes either way")

    p = with_offline(with_project(cmd("check", "Tick or untick a checklist item.", _cmd_check)))
    p.add_argument("taskId")
    p.add_argument("itemId")
    p.add_argument("--uncheck", action="store_true")

    item = cmd("item", "Manage a task's checklist items.", _cmd_item)
    isub = item.add_subparsers(dest="action", required=True, metavar="<action>")

    def item_action(name: str, help_text: str) -> _Parser:
        # --project/--offline go on each SUBcommand, not the parent: argparse only
        # accepts a parent's optionals before the subcommand word, and
        # `item add <id> foo --offline` reads far more naturally than the reverse.
        child = isub.add_parser(name, help=help_text, parents=[shared])
        child.add_argument("taskId")
        return with_offline(with_project(child))

    ia = item_action("add", "Append a checklist item.")
    ia.add_argument("title", nargs="+")
    ir = item_action("rename", "Retitle a checklist item.")
    ir.add_argument("itemId")
    ir.add_argument("title", nargs="+")
    ix = item_action("remove", "Delete a checklist item.")
    ix.add_argument("itemId")
    ic = item_action("check", "Tick or untick a checklist item.")
    ic.add_argument("itemId")
    ic.add_argument("--uncheck", action="store_true")
    im = item_action("move", "Reorder a checklist item.")
    im.add_argument("itemId")
    im.add_argument("to", type=int, help="new zero-based position")

    project = cmd("project", "Manage projects.", _cmd_project)
    psub = project.add_subparsers(dest="action", required=True, metavar="<action>")
    pa = psub.add_parser("add", help="Create a project.", parents=[shared])
    pa.add_argument("name")
    pa.add_argument("--color", default=None, metavar="HEX")
    pa.add_argument("--view-mode", default=None, choices=("list", "kanban", "timeline"))
    pa.add_argument("--kind", default=None, choices=("TASK", "NOTE"))
    pr = psub.add_parser("rename", help="Rename a project.", parents=[shared])
    pr.add_argument("id")
    pr.add_argument("name")
    pd = psub.add_parser("delete", help="Delete a project and everything in it.", parents=[shared])
    pd.add_argument("id")
    pd.add_argument("--yes", action="store_true", required=False)
    psub.add_parser("groups", help="List project groups (the folders projects sit in).", parents=[shared])

    p = cmd("parse", "Dry-run the natural-language parser.", _cmd_parse)
    p.add_argument("text", nargs="+")

    cmd("selftest", "Run offline assertions over the pure helpers.", _cmd_selftest)
    return parser


# --- shared helpers ----------------------------------------------------------


def _client(cfg: dict) -> api.Client:
    """Build an authenticated client, refusing early on a token we know is stale."""
    if cfg.get("access_token") and config.is_expired(cfg):
        raise AuthError("access token has expired — run `ticktick login` again")
    return api.Client(config.token(cfg), base=api.resolve_base(cfg))


def _remember(cfg: dict, dirty: bool = True) -> None:
    """Persist the config, treating failure as acceptable: it is only a cache."""
    if not dirty:
        return
    try:
        config.save(cfg)
    except OSError:
        pass


def _meta(catalogue: list[dict], pid: str, cfg: dict) -> tuple[str, str]:
    """(name, colour) for a project id, from an already-fetched catalogue."""
    for entry in catalogue:
        if entry["id"] == pid:
            return entry["name"], entry["color"]
    if pid and pid == str(cfg.get("inbox_id") or ""):
        return views.INBOX_NAME, ""
    return "", ""


def _scan_for_task(client: api.Client, cfg: dict, task_id: str) -> str:
    """Find the project holding `task_id`, caching every mapping the scan reveals.

    Raises:
        NotFoundError: no open task with that id. Completed tasks are invisible here —
        `/project/{id}/data` only returns undone ones.
    """
    pids = [entry["id"] for entry in views.catalogue(client, cfg)]
    found = ""
    for pid, blob in client.project_data_many(pids).items():
        for task in blob.get("tasks") or []:
            tid = str(task.get("id") or "")
            if tid:
                config.cache_project(cfg, tid, pid)
                if tid == task_id:
                    found = pid
    _remember(cfg)
    if not found:
        raise NotFoundError(
            f"no open task {task_id!r} in any project "
            "(completed tasks cannot be looked up through the Open API)"
        )
    return found


def _with_project(
    client: api.Client,
    cfg: dict,
    task_id: str,
    explicit: str | None,
    operation: Callable[[str], Any],
) -> tuple[str, Any]:
    """Run `operation(projectId)`, resolving the project id and healing a stale guess.

    Order: the flag, then the `taskId -> projectId` cache, then a full scan. A cached
    or hand-typed id that 404s falls through to the scan rather than surfacing, because
    tasks move between projects behind our back.
    """
    guess = str(explicit or config.cached_project(cfg, task_id) or "")
    if guess:
        try:
            result = operation(guess)
        except NotFoundError:
            pass
        else:
            config.cache_project(cfg, task_id, guess)
            _remember(cfg)
            return guess, result
    pid = _scan_for_task(client, cfg, task_id)
    return pid, operation(pid)


def _clean_tag(value: str) -> str:
    """A tag as the API stores it: lowercase, no leading '#'."""
    return str(value or "").strip().lstrip("#").lower()


def _merge_tags(
    current: Any,
    replace: list[str] | None,
    add: list[str] | None,
    remove: list[str] | None,
) -> list[str]:
    """The task's new tag list. `--tag` replaces; `--add-tag`/`--remove-tag` adjust.

    Order is preserved because TickTick shows tags in the order the array holds them,
    and a set would shuffle them on every edit.
    """
    base = [_clean_tag(t) for t in current] if isinstance(current, list) else []
    if replace is not None:
        base = [_clean_tag(t) for t in replace]
    for tag in add or []:
        if _clean_tag(tag) and _clean_tag(tag) not in base:
            base.append(_clean_tag(tag))
    drop = {_clean_tag(t) for t in remove or []}
    return [t for t in base if t and t not in drop]


def _cached_task(state: dict, task_id: str) -> dict | None:
    """The cached copy of a task, outbox edits included, or None."""
    for task in store.replay(state):
        if str(task.get("id") or "") == task_id:
            return task
    return None


def _project_for(
    client: api.Client | None, state: dict, cfg: dict, task_id: str, explicit: str | None
) -> str:
    """Which project holds `task_id`: the flag, the cache, then a scan.

    The cache answers first and answers offline, which is the whole point — the last
    refresh already told us where every task lives, so a `complete` needs no lookup
    round-trip and no network at all.

    Raises:
        NotFoundError: nothing knows this task and there is no way to ask.
    """
    guess = str(explicit or "").strip()
    if guess:
        return guess
    task = _cached_task(state, task_id)
    if task is not None:
        # Known locally but with no project yet — a task created offline and filed in
        # the Inbox by omission. That is an answer, not a miss: an empty projectId is
        # what "Inbox" looks like on the wire, and the create will fill it in.
        return str(task.get("projectId") or "")
    cached = config.cached_project(cfg, task_id)
    if cached:
        return cached
    if client is None:
        raise NotFoundError(
            f"no cached project for task {task_id!r}, and there is no connection to "
            "look it up — pass --project, or retry when you are back online"
        )
    return _scan_for_task(client, cfg, task_id)


def _sync(cfg: dict, state: dict, *, offline: bool = False) -> tuple[api.Client | None, dict]:
    """Push whatever is queued, and report what happened.

    Called at the top of every mutation so a write attempted while offline is not
    merely queued but retried at the next opportunity, without needing a daemon.
    """
    info: dict[str, Any] = {"pending": store.pending(state), "synced": 0, "warning": ""}
    if offline:
        info["offline"] = True
        return None, info
    try:
        client = _client(cfg)
    except AuthError:
        if not state.get("fetched_at") and not store.pending(state):
            raise
        # Signed out with work still queued: keep it. Signing back in flushes it.
        info["warning"] = "not signed in"
        return None, info
    flushed = store.flush(state, client)
    info["synced"] = flushed["synced"]
    info["pending"] = flushed["remaining"]
    if flushed["error"]:
        info["warning"] = flushed["error"]
    if flushed["blocked"]:
        info["retryAfter"] = flushed["blocked"]
    return client, info


def _push(client: api.Client | None, state: dict) -> dict:
    """Try to drain the outbox right now; report what is left.

    A mutation is already applied locally and already durable by the time this runs,
    so failure here is a delay, not a loss — which is why nothing it raises escapes.
    """
    if client is None:
        return {"pending": store.pending(state), "synced": 0}
    flushed = store.flush(state, client)
    out: dict[str, Any] = {"pending": flushed["remaining"], "synced": flushed["synced"]}
    if flushed["error"]:
        out["warning"] = flushed["error"]
    if flushed["blocked"]:
        out["retryAfter"] = flushed["blocked"]
    return out


def _read_source(
    cfg: dict,
    state: dict,
    *,
    offline: bool = False,
    force: bool = False,
    max_age: float = store.DEFAULT_MAX_AGE,
) -> tuple[Any, dict]:
    """A client-shaped object for `views.collect`, freshened when that is possible.

    Always returns the cache. The network only ever updates it, so a read is instant
    and total: a dead link, an expired token or a rate limit degrades to the last
    known-good list with a warning attached, never to an empty widget.
    """
    client, info = _sync(cfg, state, offline=offline)
    info["source"] = "cache"
    info["stale"] = not store.is_fresh(state, max_age)

    if client is not None and (force or info["stale"]):
        try:
            store.remember(state, client.all_undone(), client.projects())
            info["source"] = "network"
            info["stale"] = False
        except AuthError:
            raise  # a bad token is worth surfacing; stale data would hide it
        except (NetworkError, ApiError) as exc:
            # `fetched_at`, not `tasks`: an account whose every task is done has an
            # empty list and a perfectly good cache, and so does one where the user
            # just ticked the last box. Treating empty as "no cache" turns a
            # degraded refresh into a hard failure exactly when it matters least.
            if not state.get("fetched_at"):
                raise
            info["warning"] = info["warning"] or str(exc)
    info["pending"] = store.pending(state)
    info["age"] = round(store.age(state), 1) if state.get("fetched_at") else -1
    return store.CachedSource(state), info


def _local_tz_name() -> str:
    """The IANA zone name for this machine, or '' when it cannot be determined.

    Python exposes no portable way to ask; TZ and the /etc/localtime symlink cover
    every configuration Omarchy ships with. TickTick only uses `timeZone` to decide
    what an all-day date means, so '' (field omitted) is a safe answer, not a broken one.
    """
    name = os.environ.get("TZ", "").strip().lstrip(":")
    if not name:
        try:
            parts = pathlib.Path("/etc/localtime").resolve().parts
            if "zoneinfo" in parts:
                name = "/".join(parts[parts.index("zoneinfo") + 1:])
        except OSError:
            return ""
    try:
        ZoneInfo(name)
    except Exception:
        return ""
    return name


def _when(text: str, label: str) -> tuple[datetime, bool]:
    """Turn a `--due`-style string into (datetime, all_day) via the NL parser."""
    parsed = nlp.parse(text)
    if parsed.due is None:
        raise UsageError(f"could not understand {label} {text!r} as a date or time")
    return parsed.due, parsed.all_day


def _wire_date(due: datetime, *, all_day: bool = False) -> str:
    """Format a datetime for the API, refusing anything without a UTC offset.

    A bare `2026-08-06` is accepted with 200 OK and then silently discarded server-side:
    the task comes back with `dueDate: null` and nobody finds out until the widget shows
    an undated task. Every date this CLI puts on the wire goes through here so that
    failure mode is impossible rather than merely unlikely.
    """
    text = dates.format(due, all_day=all_day)
    if not _OFFSET_RE.search(text):
        raise UsageError(
            f"refusing to send {text!r}: TickTick silently discards a date with no UTC offset"
        )
    return text


def _due_fields(due: datetime, all_day: bool) -> dict:
    fields = {"dueDate": _wire_date(due, all_day=all_day), "isAllDay": all_day}
    zone = _local_tz_name()
    if zone:
        fields["timeZone"] = zone
    return fields


def _reminder(spec: str, *, all_day: bool = False) -> str:
    """Normalise a reminder shorthand into a TickTick trigger string.

    A trigger is an offset from the task's date, negative meaning "before it". All-day
    tasks measure from **midnight** of the due date, which is why their forms look
    inside-out: 09:00 on the day itself is `P0DT9H0M0S`, and the day before at 09:00 is
    `-P0DT15H0M0S` — fifteen hours back from midnight, not one day back.

    ponytail: raw `TRIGGER:` values pass through untouched, which is the escape hatch
    for any spelling TickTick invents that this table has not caught up with.
    """
    text = (spec or "").strip()
    if not text:
        raise UsageError("--remind needs a value")
    if text.upper().startswith("TRIGGER:"):
        return "TRIGGER:" + text[len("TRIGGER:"):]
    lowered = text.lower().replace(" ", "")
    if lowered in ("0", "ontime", "on-time", "now"):
        return "TRIGGER:PT0S"

    clock = _CLOCK_RE.match(lowered)
    if clock and 1 <= int(clock.group(1)) <= 12:
        if not all_day:
            raise UsageError(
                f"a wall-clock reminder like {spec!r} only means something on an all-day "
                "task — a timed task takes an offset such as '15m' or '1h'"
            )
        hour = int(clock.group(1)) % 12 + (12 if clock.group(2) == "pm" else 0)
        return f"TRIGGER:P0DT{hour}H0M0S"

    unit, amount = lowered[-1:], lowered[:-1]
    if unit in ("m", "h", "d") and amount.isdigit() and int(amount) > 0:
        count = int(amount)
        if unit == "m":
            return f"TRIGGER:-PT{count}M"
        if unit == "h":
            # TickTick emits hours as minutes: one hour before is -PT60M, never -PT1H.
            return f"TRIGGER:-PT{count * 60}M"
        # Whole days before an all-day task fire at 09:00, i.e. 15h back from midnight,
        # so "1 day before" is a same-day offset and the day count is one lower.
        return f"TRIGGER:-P{count - 1}DT15H0M0S" if all_day else f"TRIGGER:-P{count}D"
    raise UsageError(
        f"unrecognised reminder {spec!r} — use 'on time', '15m', '1h', '1d', "
        "'9am' on an all-day task, or a raw TRIGGER:… value"
    )


def _repeat(spec: str) -> str:
    """Normalise a repeat shorthand into an RRULE, reusing the quick-add vocabulary."""
    text = (spec or "").strip()
    if text.upper().startswith("RRULE:"):
        return text
    rule = nlp.parse("x *" + text.lower()).repeat
    if not rule:
        raise UsageError(
            f"unrecognised repeat {spec!r} — use daily/weekly/monthly/yearly/weekdays, "
            "'every 3 days', or a raw RRULE:… value"
        )
    return rule


def _resolve_project_name(catalogue: list[dict], name: str) -> str:
    """Project id for a `#name` token: exact, then prefix, then substring; longest wins."""
    needle = (name or "").strip().lower()
    if not needle:
        return ""
    tiers = (
        lambda candidate: candidate == needle,
        lambda candidate: candidate.startswith(needle),
        lambda candidate: needle in candidate,
    )
    for matches in tiers:
        hits = [p for p in catalogue if matches(p["name"].lower())]
        if hits:
            return max(hits, key=lambda p: (len(p["name"]), p["name"]))["id"]
    return ""


def _post_task(client: api.Client, task: dict, project_id: str) -> dict:
    """Write a whole task back. TickTick has no PATCH: the body replaces the task."""
    tid = str(task.get("id") or "")
    body = {k: v for k, v in task.items() if k not in ("id", "projectId")}
    return client.update_task(tid, id=tid, projectId=project_id, **body)


def _move_task(client: api.Client, cfg: dict, task_id: str, source: str, target: str) -> bool:
    """Move `task_id` from `source` to `target` through `POST /task/move`.

    That endpoint is the only supported way to change a task's project. Rewriting
    `projectId` in a `POST /task/{id}` body answers 200 without moving anything, which
    is exactly the kind of failure nobody notices until the task is missing.
    """
    if not target or target == source:
        return False
    client.move_tasks([{"fromProjectId": source, "toProjectId": target, "taskId": task_id}])
    config.cache_project(cfg, task_id, target)
    _remember(cfg)
    return True


# --- commands ----------------------------------------------------------------


def _cmd_auth(args: argparse.Namespace) -> int:
    """Human-facing: run the OAuth flow, printing progress as prose."""
    cfg = config.load()
    say = lambda message: print(message, file=sys.stderr)  # noqa: E731
    try:
        summary = auth.authorize(
            client_id=args.client_id or cfg.get("client_id") or "",
            client_secret=args.client_secret or cfg.get("client_secret") or "",
            redirect_uri=args.redirect or cfg.get("redirect_uri") or auth.DEFAULT_REDIRECT,
            no_browser=args.no_browser,
            emit=say,
        )
    except KeyboardInterrupt:
        say("\naborted")
        return 130
    except TickTickError as exc:
        say(f"error: {exc}")
        return 1
    say("")
    say("Signed in. Try `ticktick tasks --view today`.")
    if not summary["inbox_id"]:
        # Only a label: the Inbox id is read-only decoration now that tasks come back
        # from one call that already includes the Inbox.
        say("Note: no Inbox id stored (nothing in your Inbox to read it from). "
            "Tasks still load.")
    return 0


def _cmd_login(args: argparse.Namespace) -> dict:
    """Store an API token read from stdin (or `--token`), after verifying it.

    Unlike `auth`, this prints JSON and never blocks on a browser, so the widget can
    drive it directly: write the token to the process's stdin, read the answer.
    Keeping the token off argv is the point — anything in argv is world-readable
    through `/proc/<pid>/cmdline` for as long as the process lives.
    """
    if args.api_base:
        # Stored before the token is checked, because the check has to go to the
        # right host — a dida365 token verified against ticktick.com is just a 401.
        cfg = config.load()
        cfg["api_base"] = args.api_base.rstrip("/")
        config.save(cfg)

    if args.token is not None:
        token = args.token
    elif sys.stdin is None or sys.stdin.isatty():
        # A human ran this with no pipe. Asking on stderr keeps stdout pure JSON.
        print("Paste your TickTick API token (Settings -> Account -> API Token): ",
              end="", file=sys.stderr, flush=True)
        token = sys.stdin.readline() if sys.stdin else ""
    else:
        token = sys.stdin.read()

    result = auth.login_with_token(token)
    return {
        "authed": True,
        "method": "token",
        "inboxId": result["inbox_id"],
        "projects": result["projects"],
        "configPath": str(config.CONFIG_PATH),
        "warning": result["inbox_error"],
        "message": "signed in",
    }


def _cmd_logout(args: argparse.Namespace) -> dict:
    """Drop the stored credentials, and the cached tasks along with them."""
    had = auth.logout()
    # The cache holds task titles; signing out has to take them too, or the next
    # person at this machine reads them straight out of the state file.
    try:
        store.STATE_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    return {"authed": False, "wasAuthed": had, "message": "signed out"}


def _cmd_status(args: argparse.Namespace) -> dict:
    """Sign-in state plus, when reachable, the current bucket counts."""
    cfg = config.load()
    expires_at = cfg.get("expires_at")
    out: dict[str, Any] = {
        "version": __version__,
        "configPath": str(config.CONFIG_PATH),
        "authed": bool(cfg.get("access_token")),
        "expired": config.is_expired(cfg),
        "expiresAt": expires_at if isinstance(expires_at, (int, float)) else 0,
        "inboxId": str(cfg.get("inbox_id") or ""),
        "cachedTasks": len(cfg.get("project_cache") or {}),
        "projects": 0,
        "counts": {key: 0 for key in
                   ("overdue", "today", "tomorrow", "upcoming", "later", "undated", "total")},
        "warning": "",
        "message": "",
    }
    if not out["authed"]:
        out["message"] = "not signed in — run `ticktick login`"
        return out
    if out["expired"]:
        out["message"] = "token expired — run `ticktick login`"
        return out
    state = store.load()
    out["pending"] = store.pending(state)
    out["statePath"] = str(store.STATE_PATH)
    try:
        source, info = _read_source(cfg, state, force=True)
        result = views.collect(source, cfg, view="all", include_undated=True)
    except TickTickError as exc:
        # Signed in but unreachable is a different state from signed out; say so rather
        # than failing the whole command.
        out["warning"] = str(exc)
        if isinstance(exc, AuthError):
            out["authed"] = False
            out["message"] = "stored token was rejected — run `ticktick login`"
        return out
    _remember(cfg, result["cache_dirty"])
    store.save_quietly(state)
    out["counts"] = result["counts"]
    out["projects"] = len(result["projects"])
    out["source"] = info["source"]
    out["pending"] = info["pending"]
    out["warning"] = info["warning"]
    out["message"] = "signed in"
    return out


def _cmd_projects(args: argparse.Namespace) -> dict:
    cfg = config.load()
    state = store.load()
    source, info = _read_source(cfg, state, offline=args.offline)
    result = views.collect(source, cfg, view="all", include_undated=True)
    _remember(cfg, result["cache_dirty"])
    store.save_quietly(state)
    return {
        "projects": result["projects"],
        "inboxId": str(cfg.get("inbox_id") or ""),
        **info,
    }


def _cmd_tags(args: argparse.Namespace) -> dict:
    """List the account's tags.

    Undocumented in the published API but live-verified. Tags form a tree through
    `parent`, and `name` (lowercased) is the key that appears in a task's `tags`
    array while `label` is what the user typed.
    """
    cfg = config.load()
    client = _client(cfg)
    tags = client.tags()
    return {
        "tags": [
            {
                "name": str(t.get("name") or ""),
                "label": str(t.get("label") or t.get("name") or ""),
                "color": str(t.get("color") or ""),
                "parent": str(t.get("parent") or ""),
                "sortOrder": t.get("sortOrder") or 0,
            }
            for t in tags
        ],
        "count": len(tags),
    }


def _cmd_search(args: argparse.Namespace) -> dict:
    """Search every task in the account, completed ones included.

    `POST /task/search` reaches far past the undone list the widget caches, which is
    what makes it worth a separate verb: the local `--search` filter can only match
    what has already been fetched.
    """
    cfg = config.load()
    state = store.load()
    client = _client(cfg)
    text = " ".join(args.text).strip()
    status = None if args.all_states else [0]
    tasks = client.search_tasks(text, status=status)
    catalogue = views.catalogue(store.CachedSource(state), cfg)
    now = dates.now()
    rows = []
    for task in tasks:
        pid = str(task.get("projectId") or "")
        name, color = _meta(catalogue, pid, cfg)
        row = views.row(task, project_name=name, project_color=color, now=now,
                        fallback_project_id=pid)
        if _int_or_zero(task.get("status")) > 0:
            row["bucket"] = "completed"
        rows.append(row)
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]
    return {"tasks": rows, "query": text, "count": len(rows)}


def _cmd_tasks(args: argparse.Namespace) -> dict:
    """The widget's main read. Answers from the local cache, refreshed when it can be."""
    cfg = config.load()
    state = store.load()
    view = _VIEW_ALIASES.get(args.view, args.view)
    source, info = _read_source(
        cfg,
        state,
        offline=args.offline,
        force=args.refresh,
        max_age=args.max_age if args.max_age is not None else store.DEFAULT_MAX_AGE,
    )
    result = views.collect(
        source,
        cfg,
        view=view,
        project=args.project,
        upcoming_days=args.days,
        search=args.search,
        priority=args.priority,
        tags=args.tag,
        include_undated=args.include_undated,
        limit=args.limit,
    )
    _remember(cfg, result.pop("cache_dirty"))
    store.save_quietly(state)
    return {"view": view, "project": args.project or "", **result, **info}


def _cmd_sync(args: argparse.Namespace) -> dict:
    """Flush queued mutations and refresh the cache. What a poll does, on demand."""
    cfg = config.load()
    state = store.load()
    _, info = _read_source(cfg, state, force=True)
    store.save_quietly(state)
    return {
        "pending": store.pending(state),
        "outbox": [
            {
                "op": str(entry.get("op") or ""),
                "taskId": str(entry.get("taskId") or ""),
                "attempts": int(entry.get("attempts") or 0),
                "error": str(entry.get("error") or ""),
            }
            for entry in state.get("outbox") or []
        ],
        **info,
    }


def _cmd_task(args: argparse.Namespace) -> dict:
    cfg = config.load()
    client = _client(cfg)
    pid, task = _with_project(
        client, cfg, args.taskId, args.project, lambda p: client.task(p, args.taskId)
    )
    name, color = _meta(views.catalogue(client, cfg), pid, cfg)
    return {
        "task": views.row(
            task, project_name=name, project_color=color, fallback_project_id=pid
        )
    }


def _cmd_add(args: argparse.Namespace) -> dict:
    text = " ".join(args.text).strip()
    if not text:
        raise UsageError("nothing to add")
    parsed = nlp.parse(text)
    if not parsed.title:
        raise UsageError(f"{text!r} is all metadata and no title")

    # Everything that can be rejected offline is rejected before the first request.
    fields: dict[str, Any] = {"title": parsed.title}
    priority = args.priority if args.priority is not None else parsed.priority
    if priority is not None:
        fields["priority"] = priority
    if args.content:
        fields["content"] = args.content
    repeat = _repeat(args.repeat) if args.repeat else parsed.repeat
    if repeat:
        fields["repeatFlag"] = repeat

    if args.due:
        due, all_day = _when(args.due, "--due")
    else:
        due, all_day = parsed.due, parsed.all_day
    if due is not None:
        if args.all_day:
            all_day = True
        fields.update(_due_fields(due, all_day))
    if args.remind:
        # The reminder spelling depends on all_day, so this has to follow the due date.
        if due is None:
            raise UsageError(
                "a reminder needs a due date — TickTick accepts reminders on an undated "
                "task and then silently drops them; add --due (or a date in the text)"
            )
        fields["reminders"] = [_reminder(spec, all_day=all_day) for spec in args.remind]

    if args.tag:
        fields["tags"] = _merge_tags(None, args.tag, None, None)
    if args.parent:
        fields["parentId"] = str(args.parent)
    if args.start:
        start, start_all_day = _when(args.start, "--start")
        fields["startDate"] = _wire_date(start, all_day=start_all_day)

    cfg = config.load()
    state = store.load()
    client, info = _sync(cfg, state, offline=args.offline)
    # The catalogue comes out of the cache, so `#work` still resolves with no network.
    catalogue = views.catalogue(store.CachedSource(state), cfg)
    unresolved = ""
    pid = str(args.project or "")
    if not pid and parsed.project:
        pid = _resolve_project_name(catalogue, parsed.project)
        # An unknown #name must not lose the task: file it in the default project and
        # report the miss so the UI can offer to create that project.
        unresolved = "" if pid else parsed.project
    if pid:
        fields["projectId"] = pid

    # The task exists locally, with a locally-minted id, before anything is sent. If
    # the send succeeds the server's copy — real id and all — replaces it; if it does
    # not, the task is still there and still queued.
    temp = store.new_local_id()
    draft = dict(fields)
    draft["id"] = temp
    draft["status"] = 0
    if pid:
        draft["projectId"] = pid
    store.enqueue(state, {"op": "create", "taskId": temp, "projectId": pid, "task": draft})
    info.update(_push(client, state))

    # After a successful push the cache holds the server's copy under its real id;
    # after a failed one it still holds the draft. Either way, echo what is stored.
    stored = _cached_task(state, temp) or draft
    if not _cached_task(state, temp):
        for task in store.replay(state):
            if task.get("title") == draft.get("title") and not store.is_local_id(task.get("id")):
                stored = task
                break
    pid = str(stored.get("projectId") or pid)
    tid = str(stored.get("id") or "")
    if tid and pid and not store.is_local_id(tid):
        config.cache_project(cfg, tid, pid)
        _remember(cfg)
    store.save_quietly(state)
    name, color = _meta(catalogue, pid, cfg)
    return {
        "task": views.row(stored, project_name=name, project_color=color, fallback_project_id=pid),
        "parsed": _parsed_json(text, parsed),
        "unresolvedProject": unresolved,
        **info,
    }


def _cmd_edit(args: argparse.Namespace) -> dict:
    if args.clear_due and args.due:
        raise UsageError("--due and --clear-due contradict each other")
    changes: dict[str, Any] = {}
    if args.title is not None:
        changes["title"] = args.title
    if args.content is not None:
        changes["content"] = args.content
    if args.priority is not None:
        changes["priority"] = args.priority
    if args.due:
        changes.update(_due_fields(*_when(args.due, "--due")))
    if args.clear_due:
        changes.update({"dueDate": None, "startDate": None, "isAllDay": False})
    if args.start:
        start, start_all_day = _when(args.start, "--start")
        changes["startDate"] = _wire_date(start, all_day=start_all_day)
    if args.parent is not None:
        # "" detaches; the field has to be sent either way, so None is not usable here.
        changes["parentId"] = str(args.parent)
    if not changes and not (args.move_to or args.tag or args.add_tag or args.remove_tag):
        raise UsageError(
            "nothing to change — pass --title/--due/--start/--priority/--content/"
            "--tag/--parent/--move-to"
        )

    cfg = config.load()
    state = store.load()
    client, info = _sync(cfg, state, offline=args.offline)
    pid = _project_for(client, state, cfg, args.taskId, args.project)
    task = _task_body(client, state, pid, args.taskId)

    if args.tag is not None or args.add_tag or args.remove_tag:
        changes["tags"] = _merge_tags(task.get("tags"), args.tag, args.add_tag, args.remove_tag)

    if changes:
        # The whole task travels with the edit: TickTick has no PATCH, so the body
        # replaces the task, and the body has to be complete even when the network
        # is not there to fetch it.
        store.enqueue(
            state,
            {"op": "update", "taskId": args.taskId, "projectId": pid,
             "task": task, "changes": changes},
        )
    target = str(args.move_to or "")
    moved = bool(target and target != pid)
    if moved:
        # A move is its own endpoint; rewriting projectId in an update body answers
        # 200 and moves nothing.
        store.enqueue(
            state,
            {"op": "move", "taskId": args.taskId, "projectId": pid, "toProjectId": target},
        )
        config.cache_project(cfg, args.taskId, target)
        _remember(cfg)
    info.update(_push(client, state))
    store.save_quietly(state)

    final = str(target or pid)
    updated = _cached_task(state, args.taskId) or {**task, **changes}
    name, color = _meta(views.catalogue(store.CachedSource(state), cfg), final, cfg)
    return {
        "task": views.row(
            updated, project_name=name, project_color=color, fallback_project_id=final
        ),
        "moved": moved,
        **info,
    }


def _cmd_move(args: argparse.Namespace) -> dict:
    cfg = config.load()
    client = _client(cfg)
    # Fetching first resolves (and heals) the source project id, and hands us the row to
    # echo back: /task/move answers with {id, etag} and nothing else.
    source, task = _with_project(
        client, cfg, args.taskId, args.project, lambda p: client.task(p, args.taskId)
    )
    target = str(args.toProjectId)
    moved = _move_task(client, cfg, args.taskId, source, target)
    task["projectId"] = target
    name, color = _meta(views.catalogue(client, cfg), target, cfg)
    return {
        "task": views.row(
            task, project_name=name, project_color=color, fallback_project_id=target
        ),
        "fromProjectId": source,
        "moved": moved,
    }


def _cmd_completed(args: argparse.Namespace) -> dict:
    """Recently finished work: the one endpoint that can see it.

    `/project/{id}/data` and `/task/filter` return undone tasks, so completed ones are
    invisible everywhere else. Rows carry the same keys as `tasks` (so the widget can
    reuse its delegate) with `bucket: 'completed'`; the window is measured on
    `completedTime`, not the due date.
    """
    cfg = config.load()
    client = _client(cfg)
    now = dates.now()
    days = max(1, int(args.days))
    tasks = client.completed_tasks(
        projectIds=[args.project] if args.project else None,
        startDate=_wire_date(now - timedelta(days=days)),
        endDate=_wire_date(now),
    )
    catalogue = views.catalogue(client, cfg)
    rows = []
    for task in tasks:
        pid = str(task.get("projectId") or args.project or "")
        name, color = _meta(catalogue, pid, cfg)
        done = dates.parse(task.get("completedTime"))
        rows.append(
            {
                **views.row(
                    task, project_name=name, project_color=color, now=now,
                    fallback_project_id=pid,
                ),
                "bucket": "completed",
                "completed": int(done.timestamp()) if done else 0,
                "completedLabel": dates.label(done, all_day=False, now=now),
            }
        )
    rows.sort(key=lambda item: -item["completed"])  # most recently finished first
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]
    return {"tasks": rows, "days": days, "project": args.project or "", "count": len(rows)}


def _cmd_complete(args: argparse.Namespace) -> dict:
    """Finish a task locally, then tell TickTick — in that order, on purpose."""
    cfg = config.load()
    state = store.load()
    client, info = _sync(cfg, state, offline=args.offline)
    pid = _project_for(client, state, cfg, args.taskId, args.project)
    store.enqueue(state, {"op": "complete", "taskId": args.taskId, "projectId": pid})
    info.update(_push(client, state))
    store.save_quietly(state)
    return {"id": args.taskId, "projectId": pid, "completed": True, **info}


def _cmd_delete(args: argparse.Namespace) -> dict:
    cfg = config.load()
    state = store.load()
    client, info = _sync(cfg, state, offline=args.offline)
    pid = _project_for(client, state, cfg, args.taskId, args.project)
    store.enqueue(state, {"op": "delete", "taskId": args.taskId, "projectId": pid})
    info.update(_push(client, state))
    store.save_quietly(state)
    cache = cfg.get("project_cache")
    if isinstance(cache, dict) and cache.pop(args.taskId, None):
        _remember(cfg)
    return {"id": args.taskId, "projectId": pid, "deleted": True, **info}


def _task_body(client: api.Client | None, state: dict, pid: str, task_id: str) -> dict:
    """The full task to build an update body from: cache first, network only if it must.

    Raises:
        NotFoundError: it is in neither place.
    """
    task = _cached_task(state, task_id)
    if task is not None:
        return task
    if client is None:
        raise NotFoundError(
            f"task {task_id!r} is not in the local cache and there is no connection "
            "to fetch it — retry when you are back online"
        )
    fetched = client.task(pid, task_id)
    if not fetched:
        raise NotFoundError(f"no task {task_id!r} in project {pid!r}")
    return fetched


def _item_json(item: dict) -> dict:
    """One checklist item in the row shape, with every key present."""
    return {
        "id": str(item.get("id") or ""),
        "title": str(item.get("title") or ""),
        "done": _int_or_zero(item.get("status")) > 0,
    }


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _cmd_check(args: argparse.Namespace) -> dict:
    """Tick or untick a checklist item. Kept as its own verb; `item check` is the same."""
    return _checklist(args, "check")


def _cmd_item(args: argparse.Namespace) -> dict:
    """add / rename / remove / check / move one checklist item."""
    return _checklist(args, args.action)


def _checklist(args: argparse.Namespace, action: str) -> dict:
    """Edit a task's `items` array and queue the whole task back.

    There is no per-item endpoint anywhere in the Open API — the only way to touch a
    checklist is to rewrite the task that owns it. That makes every item operation a
    read-modify-write, which is exactly why the local cache matters here: without it,
    ticking a box offline is impossible, and online it costs a round-trip before the
    box even moves.
    """
    cfg = config.load()
    state = store.load()
    client, info = _sync(cfg, state, offline=getattr(args, "offline", False))
    pid = _project_for(client, state, cfg, args.taskId, args.project)
    task = _task_body(client, state, pid, args.taskId)

    raw = task.get("items")
    items = [dict(i) for i in raw if isinstance(i, dict)] if isinstance(raw, list) else []
    item_id = str(getattr(args, "itemId", "") or "")

    def find() -> dict:
        for entry in items:
            if str(entry.get("id") or "") == item_id:
                return entry
        known = ", ".join(str(i.get("id")) for i in items) or "none"
        raise NotFoundError(
            f"task {args.taskId} has no checklist item {item_id} (has: {known})"
        )

    if action == "add":
        title = " ".join(args.title).strip() if isinstance(args.title, list) else str(args.title or "").strip()
        if not title:
            raise UsageError("a checklist item needs a title")
        # TickTick assigns real item ids server-side; a local one keeps the row
        # addressable until the write lands, exactly as with a locally-created task.
        entry = {"id": store.new_local_id(), "title": title, "status": 0,
                 "sortOrder": (max((_int_or_zero(i.get("sortOrder")) for i in items), default=0) + 1) * 1000}
        items.append(entry)
        item_id = entry["id"]
    elif action == "remove":
        target = find()
        items = [i for i in items if i is not target]
    elif action == "rename":
        title = " ".join(args.title).strip() if isinstance(args.title, list) else str(args.title or "").strip()
        if not title:
            raise UsageError("a checklist item needs a title")
        find()["title"] = title
    elif action == "move":
        target = find()
        items.remove(target)
        position = max(0, min(len(items), int(args.to)))
        items.insert(position, target)
        for index, entry in enumerate(items):
            entry["sortOrder"] = (index + 1) * 1000
    else:  # check / uncheck
        target = find()
        done = not getattr(args, "uncheck", False)
        target["status"] = 1 if done else 0
        # A lingering completedTime on an unticked item makes TickTick's own UI show
        # it as done again on the next sync.
        target["completedTime"] = dates.format(dates.now()) if done else None

    store.enqueue(
        state,
        {"op": "update", "taskId": args.taskId, "projectId": pid,
         "task": task, "changes": {"items": items}},
    )
    info.update(_push(client, state))
    store.save_quietly(state)

    updated = _cached_task(state, args.taskId) or {**task, "items": items}
    name, color = _meta(views.catalogue(store.CachedSource(state), cfg), pid, cfg)
    return {
        "task": views.row(updated, project_name=name, project_color=color, fallback_project_id=pid),
        "itemId": item_id,
        "action": action,
        "items": [_item_json(i) for i in items],
        "done": _int_or_zero((next((i for i in items if str(i.get("id") or "") == item_id), {})).get("status")) > 0,
        **info,
    }


def _cmd_project(args: argparse.Namespace) -> dict:
    if args.action == "delete" and not args.yes:
        raise UsageError(
            f"deleting project {args.id} destroys every task in it and cannot be undone — "
            "pass --yes if you mean it"
        )
    cfg = config.load()
    client = _client(cfg)
    if args.action == "groups":
        groups = client.project_groups()
        return {
            "groups": [
                {"id": str(g.get("id") or ""), "name": str(g.get("name") or ""),
                 "sortOrder": g.get("sortOrder") or 0}
                for g in groups
            ],
            "count": len(groups),
        }
    if args.action == "add":
        fields: dict[str, Any] = {"name": args.name}
        if args.color:
            fields["color"] = args.color
        if args.view_mode:
            fields["viewMode"] = args.view_mode
        if args.kind:
            fields["kind"] = args.kind
        return {"project": _project_json(client.create_project(**fields)), "created": True}
    if args.action == "rename":
        return {
            "project": _project_json(client.update_project(args.id, name=args.name)),
            "renamed": True,
        }
    client.delete_project(args.id)
    return {"id": args.id, "deleted": True}


def _cmd_parse(args: argparse.Namespace) -> dict:
    """Dry-run the quick-add parser; no network, no writes."""
    text = " ".join(args.text)
    return _parsed_json(text, nlp.parse(text))


def _parsed_json(text: str, parsed: nlp.Parsed) -> dict:
    """The Parsed dataclass as QML-safe JSON: no nulls, priority defaults to 0 (none)."""
    now = dates.now()
    return {
        "input": text,
        "title": parsed.title,
        "due": parsed.due.isoformat() if parsed.due else "",
        "dueEpoch": int(parsed.due.timestamp()) if parsed.due else 0,
        "dueLabel": dates.label(parsed.due, all_day=parsed.all_day, now=now),
        "allDay": parsed.all_day if parsed.due else False,
        "priority": parsed.priority if parsed.priority is not None else 0,
        "project": parsed.project or "",
        "repeat": parsed.repeat or "",
        "reminders": parsed.reminders or [],
        "matched": parsed.matched,
    }


def _project_json(project: Any) -> dict:
    project = project if isinstance(project, dict) else {}
    return {
        "id": str(project.get("id") or ""),
        "name": str(project.get("name") or ""),
        "color": str(project.get("color") or ""),
        "viewMode": str(project.get("viewMode") or ""),
        "kind": str(project.get("kind") or ""),
    }


# --- selftest ----------------------------------------------------------------


class _StubClient:
    """Just enough client for `views.collect` to run with no network at all."""

    def __init__(self, projects: list[dict], data: dict[str, dict]) -> None:
        self._projects, self._data = projects, data

    def projects(self) -> list[dict]:
        return self._projects

    def project_data(self, pid: str) -> dict:
        return self._data.get(pid, {})

    def inbox_data(self) -> dict:
        return self._data.get("inbox9", {})

    def all_undone(self) -> list[dict]:
        """What `/task/filter` gives the real client: one flat list, no project metadata."""
        return [task for blob in self._data.values() for task in blob.get("tasks", [])]


def _cmd_selftest(args: argparse.Namespace) -> int:
    """Human-facing: assert the pure helpers still behave, without touching the network."""
    try:
        checks = _assertions()
    except AssertionError as exc:
        print(f"selftest FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # a crash is a failure too, and must not print JSON
        print(f"selftest ERROR: {exc!r}", file=sys.stderr)
        return 1
    print(f"ok — {checks} checks passed")
    return 0


def _assertions() -> int:
    """Every offline invariant the widget depends on. Returns the number checked."""
    checks = 0
    # A pinned clock for the pure helpers; `views.collect` reads the real one, so its
    # fixtures are built from `dates.now()` further down.
    now = datetime(2026, 7, 30, 14, 0).astimezone()  # a Thursday
    tz = now.tzinfo

    def check(condition: bool, message: str) -> None:
        nonlocal checks
        assert condition, message
        checks += 1

    # -- dates
    check(dates.parse(None) is None, "parse(None) must be None")
    check(dates.parse("nonsense") is None, "malformed timestamps must be None, not raise")
    check(
        dates.parse("2026-08-14T09:00:00+0000") == datetime(2026, 8, 14, 9, tzinfo=timezone.utc),
        "timed parse must keep the instant",
    )
    stamp = datetime(2026, 8, 14, 9, 0, tzinfo=tz)
    check(dates.parse(dates.format(stamp)) == stamp, "format/parse must round-trip")
    check(dates.bucket(None, all_day=False, now=now, upcoming_days=7) == "undated", "no due -> undated")
    check(
        dates.bucket(now - timedelta(hours=1), all_day=False, now=now, upcoming_days=7) == "overdue",
        "a timed task in the past is overdue",
    )
    check(
        dates.bucket(datetime.combine(now.date(), _time(), tzinfo=tz),
                     all_day=True, now=now, upcoming_days=7) == "today",
        "an all-day task is never overdue on its own day",
    )
    check(
        dates.bucket(now + timedelta(days=1), all_day=False, now=now, upcoming_days=7) == "tomorrow",
        "tomorrow bucket",
    )
    check(
        dates.bucket(now + timedelta(days=30), all_day=False, now=now, upcoming_days=7) == "later",
        "beyond the window is later",
    )
    check(
        dates.label(datetime.combine(now.date(), _time(), tzinfo=tz), all_day=True, now=now) == "today",
        "all-day today labels as 'today'",
    )
    check(dates.label(None, all_day=True, now=now) == "", "no due -> empty label")
    check(
        sorted(dates.BUCKET_ORDER, key=dates.BUCKET_ORDER.get)
        == ["overdue", "today", "tomorrow", "upcoming", "later", "undated"],
        "group order is fixed",
    )

    # -- nlp
    parsed = nlp.parse("submit report tomorrow 5pm !high #work *daily", now=now)
    check(parsed.title == "submit report", f"title stripped clean, got {parsed.title!r}")
    check(parsed.priority == 5, "!high -> 5")
    check(parsed.project == "work", "#work -> work")
    check(parsed.repeat == "RRULE:FREQ=DAILY;INTERVAL=1", "*daily -> RRULE")
    check(parsed.due is not None and parsed.due.hour == 17, "5pm -> 17:00")
    check(parsed.all_day is False, "a time makes it not all-day")
    check(nlp.parse("call #1 supplier", now=now).title == "call #1 supplier",
          "ambiguous tokens stay in the title")
    check(nlp.parse("").title == "", "empty input never raises")
    check(nlp.parse("buy milk", now=now).due is None, "no date -> no due")

    # -- views (collect reads the wall clock itself, so anchor the fixtures to it)
    real = dates.now()
    today_iso = dates.format(datetime.combine(real.date(), _time(), tzinfo=real.tzinfo),
                             all_day=True)
    past_iso = dates.format(
        datetime.combine(real.date() - timedelta(days=3), _time(), tzinfo=real.tzinfo),
        all_day=True,
    )
    client = _StubClient(
        [
            {"id": "p1", "name": "Work", "color": "#ff0000"},
            {"id": "p2", "name": "Archive", "color": "", "closed": True},
        ],
        {
            "p1": {
                "project": {"id": "p1", "name": "Work", "color": "#ff0000"},
                "tasks": [
                    {"id": "t1", "projectId": "p1", "title": "late thing",
                     "isAllDay": True, "dueDate": past_iso, "priority": 1},
                    {"id": "t2", "projectId": "p1", "title": "today thing",
                     "isAllDay": True, "dueDate": today_iso, "priority": 5,
                     "items": [{"id": "i1", "title": "step", "status": 1},
                               {"id": "i2", "title": "step two", "status": 0}]},
                    {"id": "t3", "projectId": "p1", "title": "someday", "content": "haystack"},
                ],
            },
            "inbox9": {"tasks": [{"id": "t4", "projectId": "inbox9", "title": "inbox thing"}]},
        },
    )
    cfg: dict[str, Any] = {"inbox_id": "inbox9"}
    result = views.collect(client, cfg, view="today", upcoming_days=7)
    check([t["id"] for t in result["tasks"]] == ["t1", "t2"], "today view = overdue + today, in order")
    check(result["counts"]["overdue"] == 1 and result["counts"]["undated"] == 2,
          f"counts cover every fetched task, got {result['counts']}")
    check(result["counts"]["total"] == 4, "total counts everything fetched, not what is shown")
    check([p["name"] for p in result["projects"]] == ["Inbox", "Work"],
          "closed projects are dropped and the Inbox is synthesised")
    check(result["projects"][1]["count"] == 3,
          "every project is counted off the one flat task list")
    check(cfg["project_cache"]["t4"] == "inbox9" and result["cache_dirty"],
          "the taskId->projectId cache is refreshed opportunistically")

    required = {
        "id", "projectId", "project", "projectColor", "title", "content", "bucket", "due",
        "dueLabel", "dueIso", "priority", "isAllDay", "repeat", "reminders", "items",
        "itemsDone", "itemsTotal",
    }
    for item in result["tasks"]:
        check(set(item) >= required, f"row is missing {required - set(item)}")
        check(all(item[key] is not None for key in required), f"row {item['id']} has a null")
    check(views.row({})["title"] == "" and views.row({})["items"] == [],
          "a garbage task still yields a complete row")
    check(result["tasks"][1]["itemsDone"] == 1 and result["tasks"][1]["itemsTotal"] == 2,
          "checklist progress is counted")
    check(
        [t["id"] for t in views.collect(client, cfg, view="all", search="HAYSTACK",
                                        include_undated=True)["tasks"]] == ["t3"],
        "search is case-insensitive over title and content",
    )
    check(
        [t["id"] for t in views.collect(client, cfg, view="all")["tasks"]] == ["t1", "t2"],
        "undated tasks stay hidden unless asked for",
    )
    check(
        [t["id"] for t in views.collect(client, cfg, view="project", project="inbox9")["tasks"]]
        == ["t4"],
        "the project view shows that project, undated included",
    )
    check(
        len(views.collect(client, cfg, view="all", include_undated=True, limit=2)["tasks"]) == 2,
        "limit truncates after sorting",
    )
    check(
        [t["id"] for t in views.collect(client, cfg, view="all", priority=5)["tasks"]] == ["t2"],
        "priority filters exactly",
    )

    # -- cli helpers
    check(_priority("high") == 5 and _priority("p0") == 0, "priority words map to TickTick levels")
    check(_reminder("30m") == "TRIGGER:-PT30M" and _reminder("on time") == "TRIGGER:PT0S",
          "reminder shorthands normalise")
    check(_reminder("1h") == "TRIGGER:-PT60M", "TickTick spells an hour offset in minutes")
    check(_reminder("9am", all_day=True) == "TRIGGER:P0DT9H0M0S",
          "an all-day reminder is an offset from midnight, so 9am is positive")
    check(_reminder("1d", all_day=True) == "TRIGGER:-P0DT15H0M0S",
          "all-day 'day before' is 15h back from midnight, not a whole day")
    check(_reminder("TRIGGER:-P1DT15H0M0S") == "TRIGGER:-P1DT15H0M0S",
          "a raw trigger passes through untouched")
    check(_OFFSET_RE.search(_wire_date(now)) is not None,
          "every date on the wire carries a UTC offset (a bare date is discarded)")
    check(_repeat("every 2 weeks") == "RRULE:FREQ=WEEKLY;INTERVAL=2", "repeat shorthand -> RRULE")
    catalogue = [{"id": "a", "name": "Work", "color": ""},
                 {"id": "b", "name": "Work Notes", "color": ""}]
    check(_resolve_project_name(catalogue, "WORK") == "a", "exact name match beats a prefix")
    check(_resolve_project_name(catalogue, "work n") == "b", "prefix match resolves")
    check(_resolve_project_name(catalogue, "nope") == "", "an unknown name resolves to nothing")
    return checks


# --- entry point -------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Parse `argv`, run the subcommand, and print exactly one JSON object.

    Returns 0 for every JSON-emitting subcommand, whether it succeeded or not: the
    caller reads `ok` out of the payload. Only `auth` and `selftest` return non-zero.
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except UsageError as exc:
        return _fail(exc)

    _OUTPUT["command"] = args.command
    _OUTPUT["human"] = getattr(args, "force_text", False) or (
        not getattr(args, "force_json", False)
        and bool(getattr(sys.stdout, "isatty", lambda: False)())
    )

    if args.command in HUMAN_COMMANDS:
        return int(args.func(args))
    try:
        return _emit({"ok": True, **args.func(args)})
    except TickTickError as exc:
        return _fail(exc)
    except KeyboardInterrupt:
        return _fail(NetworkError("interrupted"))
    except Exception as exc:  # noqa: BLE001 - the JSON contract outranks the traceback
        return _fail(exc)


__all__ = ["main", "HUMAN_COMMANDS"]
