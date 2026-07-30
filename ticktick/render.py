"""Human-readable rendering of the CLI's JSON payloads.

The JSON contract exists for the widget, which needs one parseable object per call.
A person at a terminal needs the opposite, so the same payload is rendered twice: raw
JSON when stdout is a pipe, a formatted view when it is a terminal.

Detecting a TTY rather than adding a flag is what keeps both honest — QML redirects
stdout and therefore always gets JSON, with nothing to remember and nothing to break
if someone forgets `--json` in a new call site.

Nothing here may raise on a payload it does not recognise: a rendering bug must not
turn a successful command into a failed one. `render` falls back to pretty JSON.
"""

from __future__ import annotations

import json
import shutil
from typing import Any

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
RED = "\033[31m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
BLUE = "\033[34m"

#: TickTick's four levels, and the mark each earns on a row.
_PRIORITY_MARK = {5: (RED, "!!!"), 3: (YELLOW, "!!"), 1: (BLUE, "!"), 0: ("", "")}

_BUCKET_TITLES = {
    "overdue": "OVERDUE",
    "today": "TODAY",
    "tomorrow": "TOMORROW",
    "upcoming": "UPCOMING",
    "later": "LATER",
    "undated": "NO DATE",
    "completed": "COMPLETED",
}

_BUCKET_ORDER = ["overdue", "today", "tomorrow", "upcoming", "later", "undated", "completed"]


class Style:
    """Colour that switches itself off when nobody can see it."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if self.enabled and code else text


def render(command: str, payload: dict, *, color: bool = True, width: int = 0) -> str:
    """One payload as text. Never raises."""
    try:
        return _render(command, payload, Style(color), width or _width())
    except Exception:  # noqa: BLE001 - a formatting bug must not fail the command
        return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def _width() -> int:
    return max(40, min(120, shutil.get_terminal_size((80, 24)).columns))


def _render(command: str, payload: dict, s: Style, width: int) -> str:
    if payload.get("ok") is False:
        return s(RED, f"error ({payload.get('error', 'api')}): ") + str(payload.get("message", ""))

    if command in ("tasks", "search", "completed"):
        return _tasks(payload, s, width)
    if command == "projects":
        return _projects(payload, s)
    if command == "tags":
        return _tags(payload, s)
    if command == "status":
        return _status(payload, s)
    if command == "sync":
        return _sync(payload, s)
    if command == "parse":
        return _parse(payload, s)
    if command in ("add", "edit", "move", "task"):
        return _task_line(payload, s, width)
    if command in ("check", "item"):
        return _checklist(payload, s, width)
    if command == "complete":
        return s(GREEN, "✓ ") + f"completed {payload.get('id', '')}" + _pending(payload, s)
    if command == "delete":
        return s(RED, "✗ ") + f"deleted {payload.get('id', '')}" + _pending(payload, s)
    if command in ("login", "logout"):
        return s(GREEN, str(payload.get("message", "ok")))
    if command == "project":
        project = payload.get("project") or {}
        if payload.get("deleted"):
            return s(RED, "✗ ") + f"deleted project {payload.get('id', '')}"
        return s(GREEN, "✓ ") + f"{project.get('name', '')}  {s(DIM, project.get('id', ''))}"
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


# --- pieces -------------------------------------------------------------------


def _tasks(payload: dict, s: Style, width: int) -> str:
    rows = payload.get("tasks") or []
    if not rows:
        return s(DIM, "nothing to show") + _footer(payload, s)

    lines: list[str] = []
    seen = ""
    for row in sorted(rows, key=lambda r: _BUCKET_ORDER.index(r.get("bucket", "undated"))
                      if r.get("bucket") in _BUCKET_ORDER else 9):
        bucket = str(row.get("bucket") or "undated")
        if bucket != seen:
            seen = bucket
            lines.append("")
            lines.append(s(BOLD, _BUCKET_TITLES.get(bucket, bucket.upper())))
        lines.append(_task_row(row, s, width))
    return "\n".join(lines).strip("\n") + _footer(payload, s)


def _task_row(row: dict, s: Style, width: int) -> str:
    color, mark = _PRIORITY_MARK.get(int(row.get("priority") or 0), ("", ""))
    done = row.get("bucket") == "completed"
    box = s(GREEN, "☑") if done else "☐"

    right: list[str] = []
    if row.get("itemsTotal"):
        right.append(f"{row.get('itemsDone', 0)}/{row['itemsTotal']}")
    if row.get("project"):
        right.append(str(row["project"]))
    label = str(row.get("completedLabel") or row.get("dueLabel") or "")
    if label:
        right.append(s(RED if row.get("bucket") == "overdue" else DIM, label))
    if row.get("repeat"):
        right.append("↻")
    for tag in (row.get("tags") or [])[:3]:
        right.append(s(BLUE, f"#{tag}"))

    tail = s(DIM, " · ").join(right)
    prefix = f"  {box} "
    if mark:
        prefix += s(color, mark) + " "
    title = str(row.get("title") or "untitled")

    # Trim the title, never the metadata: the due date is why the row is on screen.
    budget = width - _visible(prefix) - _visible(tail) - 3
    if budget > 8 and len(title) > budget:
        title = title[: budget - 1] + "…"
    pad = max(1, width - _visible(prefix) - len(title) - _visible(tail) - 1)
    line = prefix + title + (" " * pad) + tail
    return s(DIM, line) if done else line


def _visible(text: str) -> int:
    """Length ignoring ANSI escapes, so padding lines up when colour is on."""
    out, escaped = 0, False
    for ch in text:
        if escaped:
            escaped = ch != "m"
        elif ch == "\033":
            escaped = True
        else:
            out += 1
    return out


def _footer(payload: dict, s: Style) -> str:
    bits = []
    counts = payload.get("counts") or {}
    if counts:
        bits.append(f"{counts.get('overdue', 0)} overdue · {counts.get('today', 0)} today "
                    f"· {counts.get('total', 0)} total")
    if payload.get("pending"):
        bits.append(s(YELLOW, f"{payload['pending']} waiting to sync"))
    if payload.get("source") == "cache":
        age = payload.get("age", -1)
        bits.append(s(DIM, "cached" + (f" {int(age)}s ago" if isinstance(age, (int, float))
                                       and age >= 0 else "")))
    if payload.get("warning"):
        # A raw transport error carries the whole URL and body. The footer is a
        # status line, not a log: say what happened and keep the row list readable.
        bits.append(s(YELLOW, _short(str(payload["warning"]))))
    return "\n\n" + s(DIM, "  " + "  ·  ".join(bits)) if bits else ""


def _short(text: str, limit: int = 72) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def _projects(payload: dict, s: Style) -> str:
    rows = payload.get("projects") or []
    if not rows:
        return s(DIM, "no projects")
    pad = max((len(str(p.get("name", ""))) for p in rows), default=0)
    lines = [
        f"  {str(p.get('name', '')):<{pad}}  {s(DIM, str(p.get('count', 0)).rjust(3))}"
        f"  {s(DIM, str(p.get('id', '')))}"
        for p in rows
    ]
    return "\n".join(lines) + _footer(payload, s)


def _tags(payload: dict, s: Style) -> str:
    rows = payload.get("tags") or []
    if not rows:
        return s(DIM, "no tags")
    # Children are shown under their parent; TickTick's tags form a tree.
    roots = [t for t in rows if not t.get("parent")]
    kids: dict[str, list] = {}
    for tag in rows:
        if tag.get("parent"):
            kids.setdefault(str(tag["parent"]), []).append(tag)
    lines = []
    for tag in roots:
        lines.append("  " + s(BLUE, "#" + str(tag.get("label") or tag.get("name"))))
        for kid in kids.get(str(tag.get("name")), []):
            lines.append("    " + s(DIM, "#" + str(kid.get("label") or kid.get("name"))))
    for parent, group in kids.items():
        if not any(t.get("name") == parent for t in roots):
            for kid in group:  # an orphan whose parent is gone still deserves a line
                lines.append("  " + s(BLUE, "#" + str(kid.get("label") or kid.get("name"))))
    return "\n".join(lines)


def _status(payload: dict, s: Style) -> str:
    if not payload.get("authed"):
        return (s(RED, "not signed in") + "\n"
                + s(DIM, "  run `ticktick login` and paste a token from\n"
                         "  TickTick → Settings → Account → API Token"))
    counts = payload.get("counts") or {}
    lines = [
        s(GREEN, "signed in") + s(DIM, f"  ({payload.get('configPath', '')})"),
        f"  {counts.get('overdue', 0)} overdue · {counts.get('today', 0)} today "
        f"· {counts.get('total', 0)} total · {payload.get('projects', 0)} projects",
    ]
    if payload.get("pending"):
        lines.append(s(YELLOW, f"  {payload['pending']} change(s) waiting to sync"))
    if payload.get("warning"):
        lines.append(s(YELLOW, f"  {payload['warning']}"))
    return "\n".join(lines)


def _sync(payload: dict, s: Style) -> str:
    lines = [f"synced {payload.get('synced', 0)}, {payload.get('pending', 0)} pending"]
    for entry in payload.get("outbox") or []:
        lines.append(s(DIM, f"  {entry.get('op')} {entry.get('taskId')} "
                            f"(attempt {entry.get('attempts')}) {entry.get('error', '')}"))
    if payload.get("retryAfter"):
        lines.append(s(YELLOW, f"  rate limited — retrying in {payload['retryAfter']:.0f}s"))
    if payload.get("warning"):
        lines.append(s(YELLOW, f"  {payload['warning']}"))
    return "\n".join(lines)


def _parse(payload: dict, s: Style) -> str:
    rows = [
        ("title", payload.get("title", "")),
        ("due", payload.get("dueLabel") or payload.get("due") or "—"),
        ("all day", "yes" if payload.get("allDay") else "no"),
        ("priority", {5: "high", 3: "medium", 1: "low", 0: "none"}.get(payload.get("priority", 0), "none")),
        ("project", payload.get("project") or "—"),
        ("repeat", payload.get("repeat") or "—"),
    ]
    lines = [f"  {s(DIM, key.rjust(9))}  {value}" for key, value in rows]
    if payload.get("matched"):
        lines.append(f"  {s(DIM, 'consumed'.rjust(9))}  {', '.join(payload['matched'])}")
    return "\n".join(lines)


def _task_line(payload: dict, s: Style, width: int) -> str:
    task = payload.get("task") or {}
    out = [_task_row(task, s, width).lstrip()]
    if payload.get("unresolvedProject"):
        out.append(s(YELLOW, f"  no project named “{payload['unresolvedProject']}” — "
                             "filed in the default list"))
    if payload.get("moved"):
        out.append(s(DIM, "  moved"))
    tail = _pending(payload, s)
    if tail:
        out.append(tail.strip())
    return "\n".join(out)


def _checklist(payload: dict, s: Style, width: int) -> str:
    task = payload.get("task") or {}
    lines = [str(task.get("title") or "")]
    for item in payload.get("items") or []:
        box = s(GREEN, "☑") if item.get("done") else "☐"
        title = str(item.get("title") or "")
        lines.append(f"  {box} " + (s(DIM, title) if item.get("done") else title))
    tail = _pending(payload, s)
    if tail:
        lines.append(tail.strip())
    return "\n".join(lines)


def _pending(payload: dict, s: Style) -> str:
    if payload.get("pending"):
        return s(YELLOW, f"  ({payload['pending']} waiting to sync)")
    return ""


__all__ = ["render", "Style"]
