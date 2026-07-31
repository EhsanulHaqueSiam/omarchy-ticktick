"""Local-first state: a task cache the UI paints from, and a durable outbox it writes to.

The widget must feel instant and keep working with no network, which the stateless
CLI could not do on its own — every invocation is a fresh process, so an optimistic
edit held only in QML died with the next poll, and a poll with no network blanked the
list. Both problems are the same missing thing: state that outlives the process.

So one JSON file at ``$XDG_STATE_HOME/ticktick/state.json`` holds

* ``tasks`` / ``projects`` — the **raw API payloads** of the last good fetch, and
* ``outbox`` — mutations that have been applied locally but not yet accepted upstream.

Raw payloads, not rendered rows: a row carries a bucket and a "today" label computed
against the clock at fetch time, so a cached row would be confidently wrong by
morning. Caching what the API said and re-deriving the view on every read is the only
version that is right at 00:01.

The read path is therefore::

    cache ──► outbox replayed on top ──► views.collect ──► rows

and the write path::

    apply to cache ──► append to outbox ──► try to flush ──► drop what stuck

:class:`CachedSource` exists so none of this leaks into `views.collect`: it answers
the same four methods :class:`ticktick.api.Client` does, out of the cache, so the view
layer never learns whether it is online.

Failure is expected here rather than exceptional. A mutation that cannot reach the
server stays queued; one the server refuses on its own terms (the task is gone, the
body was bad) is dropped with its reason recorded, because retrying it forever would
wedge every write behind it.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import secrets
import tempfile
import time
from typing import Any

try:  # POSIX only; the merge in `save` is what actually preserves concurrent work.
    import fcntl
except ImportError:  # pragma: no cover - Omarchy is Linux, but do not hard-fail
    fcntl = None  # type: ignore[assignment]

from .errors import ApiError, AuthError, NetworkError, NotFoundError, UsageError

_XDG_STATE = os.environ.get("XDG_STATE_HOME") or ""
STATE_DIR: pathlib.Path = (
    pathlib.Path(_XDG_STATE) if _XDG_STATE else pathlib.Path.home() / ".local" / "state"
) / "ticktick"
STATE_PATH: pathlib.Path = STATE_DIR / "state.json"

#: Bumped when the on-disk shape changes incompatibly; an older file is discarded
#: rather than migrated, because it is a cache and one refresh rebuilds it.
STATE_VERSION = 1

#: How long a cached fetch is served without going to the network.
DEFAULT_MAX_AGE = 45.0

#: Longest a persisted backoff may hold writes, whatever the file claims. Applied to
#: the stamp on the way in (see `load`), because it is an absolute time on a clock
#: that can move backwards.
MAX_BACKOFF = 300.0

#: Hard ceiling on queued mutations. Reached only if the network is gone for a very
#: long time; the oldest are dropped, because the newest are what the user can still see.
MAX_OUTBOX = 500

#: Locally-minted task ids. `inbox` is TickTick's only prefix and real ids are hex
#: ObjectIds, so nothing upstream can collide with this.
LOCAL_PREFIX = "local-"

#: Operations the outbox knows how to replay, in the order this module applies them.
OPS = ("create", "update", "move", "complete", "delete")


def is_local_id(task_id: Any) -> bool:
    """True for an id this machine minted for a task the server has never seen."""
    return str(task_id or "").startswith(LOCAL_PREFIX)


def new_local_id() -> str:
    """A collision-proof placeholder id for a task created with no network."""
    return LOCAL_PREFIX + secrets.token_hex(8)


# --- persistence -------------------------------------------------------------


def blank() -> dict:
    """A fresh, valid, empty state."""
    return {
        "version": STATE_VERSION,
        "fetched_at": 0.0,
        "tasks": [],
        "projects": [],
        "groups": [],
        "outbox": [],
        "retry_after": 0.0,
        "last_error": "",
    }


def load() -> dict:
    """Read the state file, or return a blank state when it is absent or unusable.

    Total by construction: this is a cache, and a widget that refuses to start
    because its cache is corrupt is worse than one that refetches.
    """
    try:
        data: Any = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return blank()
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return blank()
    state = blank()
    state.update(data)
    # Every field is re-typed rather than trusted: a hand-edited or truncated file
    # must not be able to hand a list where the rest of this module expects a dict.
    state["tasks"] = [t for t in _list(state.get("tasks")) if isinstance(t, dict)]
    state["projects"] = [p for p in _list(state.get("projects")) if isinstance(p, dict)]
    state["groups"] = [g for g in _list(state.get("groups")) if isinstance(g, dict)]
    state["outbox"] = [op for op in _list(state.get("outbox")) if _valid_op(op)]
    state["fetched_at"] = _float(state.get("fetched_at"))
    # Clamped here, where every process passes, rather than where it is read: it is an
    # absolute time on a clock that can move, and capping only the *reported* number
    # left the gate in `flush` still shut for however long the file claimed. One
    # backwards jump could otherwise park it hours out and freeze every write.
    state["retry_after"] = min(_float(state.get("retry_after")), time.time() + MAX_BACKOFF)
    state["last_error"] = str(state.get("last_error") or "")
    # What the queue held when we read it, so `save` can tell an entry we resolved
    # from one another process appended while we were away. Stripped before writing.
    state["_loaded_ops"] = [str(op.get("id") or "") for op in state["outbox"]]
    return state


def save(state: dict) -> None:
    """Write the state atomically, keeping work another process queued meanwhile.

    Two processes touch this file routinely — the widget polls on a timer while the
    user runs commands in a terminal — and each does a read, then network I/O, then a
    write. A plain last-writer-wins would silently discard whatever the other one
    queued in between, which is the one thing the outbox exists to prevent.

    Locking across the whole read-modify-write is not an option: the middle of it is a
    network round trip, and blocking a click behind a twenty-second timeout would be
    its own bug. So the merge happens here instead, under a lock held only for the
    write: re-read the file, keep any entry that appeared since we loaded, and drop
    only the ones we know we resolved.

    Same-directory temp file so ``os.replace`` is a rename rather than a copy: a
    reader sees the whole old file or the whole new one, never half of either.
    """
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _write_lock():
        merged = _merge_with_disk(state)
        fd, tmp = tempfile.mkstemp(dir=str(STATE_PATH.parent), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(merged, fh, ensure_ascii=False, default=str)
                fh.flush()
                os.fsync(fh.fileno())  # the rename is only atomic for data that landed
            os.chmod(tmp, 0o600)  # task titles are private even though tokens are not
            os.replace(tmp, STATE_PATH)
        except BaseException:
            pathlib.Path(tmp).unlink(missing_ok=True)
            raise
    # Keep the caller's copy consistent with what is now on disk.
    state["outbox"] = merged["outbox"]
    # An entry we have just written is one we have seen. Without this, an entry merged
    # in from another process stays "unknown" for the rest of this process's life, so
    # the next save merges it back in *after* we synced and dropped it — and it is
    # sent a second time. Creates are not idempotent.
    state["_loaded_ops"] = sorted(
        set(state.get("_loaded_ops") or [])
        | {str(op.get("id") or "") for op in merged["outbox"]}
    )


@contextlib.contextmanager
def _write_lock():
    """An advisory exclusive lock on a sidecar file, held only across the write."""
    if fcntl is None:  # not POSIX; the merge below still prevents the common loss
        yield
        return
    path = STATE_PATH.with_name(STATE_PATH.name + ".lock")
    handle = None
    try:
        handle = open(path, "a+b")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError:
        # A read-only or full state directory must degrade, not crash the widget.
        if handle is not None:
            handle.close()
        yield
        return
    try:
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _merge_with_disk(state: dict) -> dict:
    """`state`, plus any outbox entry another process appended since we loaded it."""
    merged = {k: v for k, v in state.items() if not k.startswith("_")}
    ours = _list(state.get("outbox"))
    known = set(state.get("_loaded_ops") or [])
    mine = {str(e.get("id") or "") for e in ours}

    on_disk = []
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        raw = None
    if isinstance(raw, dict) and raw.get("version") == STATE_VERSION:
        on_disk = [op for op in _list(raw.get("outbox")) if _valid_op(op)]

    # An entry we never saw at load time is someone else's, and still owed. One we
    # did see and no longer hold is one we resolved, so it must not come back.
    fresh = [
        op for op in on_disk
        if str(op.get("id") or "") not in known and str(op.get("id") or "") not in mine
    ]
    merged["outbox"] = ours + fresh
    while len(merged["outbox"]) > MAX_OUTBOX:
        merged["outbox"].pop(0)
    return merged


def save_quietly(state: dict) -> bool:
    """`save`, but a full or read-only disk degrades the app instead of killing it."""
    try:
        save(state)
        return True
    except OSError:
        return False


# --- cache -------------------------------------------------------------------


def age(state: dict, *, now: float | None = None) -> float:
    """Seconds since the last successful fetch; infinite when there was none.

    A stamp in the future means the clock moved — an NTP correction, a dual-boot, a
    dead RTC — and the honest answer is "no idea", which must round to *stale*.
    Clamping the difference to zero instead would mark the cache eternally fresh and
    the widget would never fetch again.
    """
    fetched = _float(state.get("fetched_at"))
    if fetched <= 0:
        return float("inf")
    elapsed = (time.time() if now is None else now) - fetched
    return elapsed if elapsed >= 0 else float("inf")


def is_fresh(state: dict, max_age: float = DEFAULT_MAX_AGE, *, now: float | None = None) -> bool:
    """Whether the cache may answer a read without touching the network."""
    return bool(state.get("tasks") or state.get("projects")) and age(state, now=now) < max_age


def remember(
    state: dict,
    tasks: list,
    projects: list,
    groups: list | None = None,
    *,
    now: float | None = None,
) -> None:
    """Replace the cache with a fresh fetch and stamp it.

    The outbox is deliberately untouched: those mutations are not in this payload yet
    (that is what makes them pending), and `replay` puts them back on top on read.

    `groups` is the folders lists are filed under, and `None` means "did not ask" —
    the folders already cached are kept rather than blanked, because the endpoint is
    undocumented and failing to read it must not empty the sidebar.
    """
    state["tasks"] = [t for t in _list(tasks) if isinstance(t, dict)]
    state["projects"] = [p for p in _list(projects) if isinstance(p, dict)]
    if groups is not None:
        state["groups"] = [g for g in _list(groups) if isinstance(g, dict)]
    state["fetched_at"] = time.time() if now is None else now
    state["last_error"] = ""


def replay(state: dict) -> list[dict]:
    """The cached tasks with every queued mutation applied on top.

    This is what a read must render. Without it a completed task reappears the moment
    a refresh lands, because the server has not been told yet — the user sees their
    own click undone, which reads as a bug even though the write is merely in flight.
    """
    tasks = [dict(t) for t in state.get("tasks") or []]
    for op in state.get("outbox") or []:
        _apply(tasks, op)
    return tasks


def apply_local(state: dict, op: dict) -> None:
    """Fold one mutation into the cache immediately, before it is sent anywhere."""
    tasks = [dict(t) for t in state.get("tasks") or []]
    _apply(tasks, op)
    state["tasks"] = tasks


# --- outbox ------------------------------------------------------------------


def enqueue(state: dict, op: dict) -> dict:
    """Append a mutation, apply it locally, and hand back the stored entry.

    Local application happens here rather than at the call site so the cache and the
    queue can never disagree about whether something was recorded.
    """
    if not _valid_op(op):
        raise UsageError(f"refusing to queue a malformed operation: {op!r}")
    entry = dict(op)
    entry.setdefault("id", "op-" + secrets.token_hex(6))
    entry.setdefault("queued_at", time.time())
    entry["attempts"] = 0
    entry["error"] = ""

    outbox = _list(state.get("outbox"))
    outbox.append(entry)
    # Dropping the oldest keeps what the user most recently did, which is the part
    # still on their screen.
    while len(outbox) > MAX_OUTBOX:
        outbox.pop(0)
    state["outbox"] = outbox
    apply_local(state, entry)
    return entry


def pending(state: dict) -> int:
    """How many mutations are still waiting to reach TickTick."""
    return len(state.get("outbox") or [])


def blocked_for(state: dict, *, now: float | None = None) -> float:
    """Seconds left on the persisted rate-limit backoff, or 0.

    Backoff has to survive the process: every CLI invocation is a new one, so an
    in-memory retry timer would let a widget polling every 30s hammer straight
    through a limit it had already been told about.

    The ceiling lives in `load`, not here: capping only the number this reports would
    leave the gate in `flush` — which reads the same stamp — shut for however long the
    file claimed.
    """
    return max(0.0, _float(state.get("retry_after")) - (time.time() if now is None else now))


def hold_off(state: dict, seconds: float, reason: str = "", *, now: float | None = None) -> None:
    """Refuse network writes for `seconds`, remembering why."""
    until = (time.time() if now is None else now) + max(0.0, float(seconds))
    state["retry_after"] = max(_float(state.get("retry_after")), until)
    if reason:
        state["last_error"] = reason


def flush(
    state: dict,
    client: Any,
    *,
    now: float | None = None,
    limit: int | None = None,
) -> dict:
    """Send queued mutations to TickTick, oldest first, and prune what settled.

    Stops at the first entry that fails for a reason retrying could fix (offline,
    throttled, token rejected) so ordering is preserved — a later edit must never
    overtake the create it depends on. An entry the server rejects on its own terms
    is dropped instead, with its reason kept for the UI.

    Returns ``{'synced', 'dropped', 'remaining', 'error', 'blocked'}``.
    """
    outbox = _list(state.get("outbox"))
    result: dict[str, Any] = {
        "synced": 0, "dropped": 0, "remaining": len(outbox),
        "error": "", "blocked": 0.0,
        # `local-` id -> the id TickTick issued. The caller needs this to say which
        # task it just created; guessing by title finds somebody else's.
        "idmap": {},
    }
    if not outbox:
        return result

    held = blocked_for(state, now=now)
    if held > 0:
        result["blocked"] = round(held, 3)
        result["error"] = state.get("last_error") or "rate limited — waiting before retrying"
        return result

    idmap: dict[str, str] = {}
    budget = len(outbox) if limit is None else max(0, int(limit))
    processed = 0

    while outbox and processed < budget:
        entry = outbox[0]
        _remap(entry, idmap)
        try:
            _send(entry, client, idmap, state)
        except (NetworkError, AuthError) as exc:
            # The request never reached the user's data, or never will until they
            # sign in again. Either way the queue is still valid, and it is kept
            # however many times this fails: a laptop shut for a fortnight, or a
            # widget polling every five minutes through a long outage, would other-
            # wise burn through an attempt budget and throw away work the user
            # watched themselves do. Growth is bounded by MAX_OUTBOX instead, which
            # discards by age rather than by luck.
            entry["attempts"] = _int(entry.get("attempts")) + 1
            entry["error"] = str(exc)
            result["error"] = str(exc)
            break
        except ApiError as exc:
            entry["attempts"] = _int(entry.get("attempts")) + 1
            entry["error"] = str(exc)
            result["error"] = str(exc)
            status = _int(getattr(exc, "status", 0))

            if getattr(exc, "throttled", False):
                # Exponential, capped, and persisted: the next process inherits it.
                hold_off(state, min(300.0, 5.0 * 2 ** min(5, entry["attempts"])), str(exc), now=now)
                result["blocked"] = round(blocked_for(state, now=now), 3)
                break

            if status >= 500 or status == 0:
                # The server broke, or never gave a verdict at all: a non-JSON 200 is
                # a captive portal's login page or an HTML gateway error, not a
                # rejection of this body, and it arrives here as status 0. Attempts
                # are burned by polls and by mutations, not by time, so a cap here
                # would count the user's activity rather than the outage — eight polls
                # is minutes. The entry stays, bounded like every other one by
                # MAX_OUTBOX, which discards by age rather than by luck.
                break

            # A genuine 4xx: this body will never be accepted, and retrying it would
            # wedge every write behind it forever.
            outbox.pop(0)
            result["dropped"] += 1
            state["last_error"] = str(exc)
            continue
        except (NotFoundError, UsageError) as exc:
            # The task is already gone, or the body was never acceptable. Completing
            # something twice is a no-op the user meant anyway.
            outbox.pop(0)
            result["dropped"] += 1
            # Reported like every other drop is: without this the caller turns a
            # write the server refused into a clean `ok: true`, and a later success
            # in the same flush wipes `last_error` so even the reason is gone.
            result["error"] = str(exc)
            state["last_error"] = str(exc)
            continue
        else:
            outbox.pop(0)
            result["synced"] += 1
        finally:
            processed += 1

    if idmap:
        _adopt_ids(state, idmap)
    result["idmap"] = dict(idmap)
    state["outbox"] = outbox
    result["remaining"] = len(outbox)
    if result["synced"] and not result["error"]:
        state["retry_after"] = 0.0
        state["last_error"] = ""
    save_quietly(state)
    return result


# --- the client face the view layer sees -------------------------------------


class CachedSource:
    """Answers `views.collect` out of the cache, with the outbox already folded in.

    Deliberately the same methods `views.collect` calls on a real
    :class:`ticktick.api.Client`, so the view layer is identical online and off. It
    is read-only: writes go through the outbox, never through here.
    """

    def __init__(self, state: dict) -> None:
        self._state = state
        self._tasks = replay(state)

    def projects(self) -> list[dict]:
        return [dict(p) for p in self._state.get("projects") or []]

    def project_groups(self) -> list[dict]:
        return [dict(g) for g in self._state.get("groups") or []]

    def all_undone(self) -> list[dict]:
        return [dict(t) for t in self._tasks]

    def project_data(self, pid: str) -> dict:
        """The `/project/{id}/data` shape, rebuilt from the flat cached list."""
        target = str(pid or "")
        project = next(
            (dict(p) for p in self._state.get("projects") or [] if str(p.get("id") or "") == target),
            None,
        )
        tasks = [dict(t) for t in self._tasks if str(t.get("projectId") or "") == target]
        return {"project": project, "tasks": tasks, "columns": []}

    def inbox_data(self) -> dict:
        """The Inbox, found the same way the live path finds it: by id prefix."""
        tasks = [
            dict(t)
            for t in self._tasks
            if str(t.get("projectId") or "").startswith("inbox")
        ]
        return {"project": None, "tasks": tasks, "columns": []}


# --- internals ---------------------------------------------------------------


def _apply(tasks: list[dict], op: dict) -> None:
    """Fold one operation into a task list in place. Never raises on odd input."""
    kind = str(op.get("op") or "")
    if kind == "create":
        task = dict(op.get("task") or {})
        task.setdefault("id", op.get("taskId") or new_local_id())
        task["_pending"] = True
        # An id already present means this op was applied before (a re-read of the
        # same outbox); replacing keeps the list idempotent under replay.
        _remove(tasks, str(task["id"]))
        tasks.append(task)
        return

    tid = str(op.get("taskId") or "")
    if not tid:
        return
    if kind in ("complete", "delete"):
        # The cache mirrors `/task/filter status=[0]`, which is undone tasks only, so
        # both finishing and destroying a task remove it from the cached set.
        _remove(tasks, tid)
        return
    for task in tasks:
        if str(task.get("id") or "") != tid:
            continue
        if kind == "update":
            task.update(dict(op.get("changes") or {}))
        elif kind == "move":
            task["projectId"] = str(op.get("toProjectId") or task.get("projectId") or "")
        task["_pending"] = True
        return


def _remove(tasks: list[dict], task_id: str) -> None:
    for index, task in enumerate(tasks):
        if str(task.get("id") or "") == task_id:
            del tasks[index]
            return


def _send(entry: dict, client: Any, idmap: dict[str, str], state: dict) -> None:
    """Perform one queued mutation against the live API."""
    kind = str(entry.get("op") or "")
    tid = str(entry.get("taskId") or "")
    # A task queued before it had a project — created offline into the Inbox — only
    # learns its real projectId when the create lands. Re-reading it from the cache
    # here is what lets the edit that followed it still address the right project.
    pid = str(entry.get("projectId") or "") or _project_of(state, tid)

    if kind == "create":
        fields = {k: v for k, v in dict(entry.get("task") or {}).items() if not k.startswith("_")}
        fields.pop("id", None)
        if is_local_id(fields.get("parentId")):
            # The parent's own create was dropped, so this id will never be minted.
            # A dangling `local-` parent is worse than none: no client can resolve it.
            fields.pop("parentId", None)
        fields["items"] = _wire_items(fields.get("items"))
        if not fields["items"]:
            fields.pop("items")
        created = client.create_task(**fields) or {}
        real = str(created.get("id") or "")
        if real and tid and real != tid:
            idmap[tid] = real
            # Adopt the server's copy wholesale: it carries the fields the API filled
            # in (sortOrder, etag, the id) that a locally-built task cannot know.
            _replace_task(state, tid, created)
        elif real:
            _replace_task(state, real, created)
        return

    if kind == "update":
        # TickTick has no PATCH: this body replaces the task, so every field it carries
        # staler than the server's is a field silently reverted — and the user only
        # ever asked to change the ones in `changes`. So the base body is the freshest
        # thing available, in this order:
        #
        #   * a retry re-reads the task from the server. It has been sitting in the
        #     queue through at least one failure, and a flush runs *before* the refetch
        #     that would have updated the cache, so the cache is exactly as stale as
        #     the snapshot is. One GET, only on the path where something has already
        #     gone wrong, is worth not reverting another device's edit.
        #   * otherwise the live cache, which `remember` keeps current;
        #   * and the snapshot frozen at enqueue time only when the cache no longer
        #     holds the task at all.
        base: dict | None = None
        if _int(entry.get("attempts")) > 0:
            try:
                base = client.task(pid, tid) or None
            except (ApiError, AuthError, NetworkError, NotFoundError):
                base = None  # not worth failing the write over; fall through
        if base is None:
            base = next(
                (t for t in _list(state.get("tasks")) if str(t.get("id") or "") == tid), None
            )
        body = {
            k: v
            for k, v in dict(base if base is not None else entry.get("task") or {}).items()
            if not k.startswith("_")
        }
        body.update(dict(entry.get("changes") or {}))
        body.pop("id", None)
        body.pop("projectId", None)
        if "items" in body:
            body["items"] = _wire_items(body.get("items"))
        updated = client.update_task(tid, id=tid, projectId=pid, **body) or {}
        if updated.get("id"):
            # The response is the only place the ids TickTick just minted for new
            # checklist items appear; without adopting it they stay local forever
            # and the next tick addresses an item the server has never heard of.
            _replace_task(state, tid, updated)
        return

    if kind == "move":
        target = str(entry.get("toProjectId") or "")
        if target and target != pid:
            client.move_tasks([{"fromProjectId": pid, "toProjectId": target, "taskId": tid}])
        return

    if kind == "complete":
        client.complete_task(pid, tid)
        return

    if kind == "delete":
        client.delete_task(pid, tid)
        return

    raise UsageError(f"unknown queued operation {kind!r}")


def _wire_items(raw: Any) -> list[dict]:
    """Checklist items ready for the wire, with locally-minted ids stripped.

    A local id is a placeholder this machine invented so the UI could address an item
    before the server knew about it. Sending it would either be rejected or, worse,
    accepted — leaving an id no other client can resolve. Dropping it asks TickTick to
    assign a real one, which the update response then hands back.
    """
    out = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        item = {k: v for k, v in entry.items() if not k.startswith("_")}
        if is_local_id(item.get("id")):
            item.pop("id", None)
        out.append(item)
    return out


def _project_of(state: dict, task_id: str) -> str:
    """The project a cached task currently lives in, or ''."""
    for task in state.get("tasks") or []:
        if str(task.get("id") or "") == task_id:
            return str(task.get("projectId") or "")
    return ""


def _remap(entry: dict, idmap: dict[str, str]) -> None:
    """Rewrite a queued entry's ids after an earlier create learned its real one.

    Every string at every depth, not an enumerated list of keys: a `parentId` inside a
    create body is as much an id as the `taskId` beside it, and naming the keys means
    the next id-bearing field somebody adds silently uploads a `local-` placeholder no
    TickTick client can resolve. A local id is 16 random hex characters, so no ordinary
    value collides with a key in here.
    """
    for key, value in entry.items():
        if isinstance(value, str):
            if value in idmap:
                entry[key] = idmap[value]
        elif isinstance(value, dict):
            _remap(value, idmap)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _remap(item, idmap)


def _adopt_ids(state: dict, idmap: dict[str, str]) -> None:
    """Point the cache and any still-queued entry at the ids the server issued."""
    for task in state.get("tasks") or []:
        tid = str(task.get("id") or "")
        if tid in idmap:
            task["id"] = idmap[tid]
    for entry in state.get("outbox") or []:
        _remap(entry, idmap)


def _replace_task(state: dict, task_id: str, fresh: dict) -> None:
    """Swap a locally-built task for the server's authoritative copy."""
    tasks = _list(state.get("tasks"))
    for index, task in enumerate(tasks):
        if str(task.get("id") or "") == task_id:
            tasks[index] = dict(fresh)
            break
    else:
        tasks.append(dict(fresh))
    state["tasks"] = tasks


def _valid_op(op: Any) -> bool:
    """Whether an outbox entry is one this module can still replay."""
    if not isinstance(op, dict) or str(op.get("op") or "") not in OPS:
        return False
    if op["op"] == "create":
        return isinstance(op.get("task"), dict)
    return bool(str(op.get("taskId") or ""))


def _list(value: Any) -> list:
    return list(value) if isinstance(value, list) else []


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "STATE_DIR",
    "STATE_PATH",
    "STATE_VERSION",
    "DEFAULT_MAX_AGE",
    "LOCAL_PREFIX",
    "CachedSource",
    "age",
    "apply_local",
    "blank",
    "blocked_for",
    "enqueue",
    "flush",
    "hold_off",
    "is_fresh",
    "is_local_id",
    "load",
    "new_local_id",
    "pending",
    "remember",
    "replay",
    "save",
    "save_quietly",
]
