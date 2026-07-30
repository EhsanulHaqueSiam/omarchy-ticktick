"""Tests for ticktick.cli, driven through a fake transport.

Every request is intercepted at `api.Client.request`, so nothing here reaches the
network or the user's account — and for several of these tests, *not writing* is the
whole assertion. TZ is pinned so the UTC offsets we format are deterministic.

The recurring theme is API-FACTS §3: TickTick answers 200 OK to a date with no UTC
offset and then silently discards it, so a bug here shows up as a task that quietly has
no due date rather than as an error anyone can see.
"""

import contextlib
import io
import json
import os
import pathlib
import re
import sys
import tempfile
import time
import unittest
from typing import Any, Iterator
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ticktick import api, auth, cli, config  # noqa: E402
from ticktick.errors import UsageError  # noqa: E402

#: Anything that starts like a date is date-shaped; the assertion is that it also ends
#: in a four-digit UTC offset.
_DATEISH = re.compile(r"^\d{4}-\d{2}-\d{2}")
_OFFSET = re.compile(r"[+-]\d{4}$")

#: POSTs that only read. Every other POST/PUT/DELETE changes the user's account.
_READ_ONLY_POSTS = ("/task/filter", "/task/completed")

PROJECTS = [
    {"id": "p1", "name": "Work", "color": "#ff0000"},
    {"id": "p2", "name": "Home", "color": ""},
]
TASK = {
    "id": "t1",
    "projectId": "p1",
    "title": "a task",
    "dueDate": "2026-08-05T18:00:00.000+0000",
    "isAllDay": True,
    "timeZone": "Europe/Berlin",
    "priority": 3,
}
COMPLETED = [
    {"id": "c1", "projectId": "p1", "title": "older", "status": 2,
     "completedTime": "2026-07-28T09:00:00.000+0000"},
    {"id": "c2", "projectId": "p2", "title": "newer", "status": 2,
     "completedTime": "2026-07-29T09:00:00.000+0000"},
]


def setUpModule() -> None:
    os.environ["TZ"] = "Europe/Berlin"
    if hasattr(time, "tzset"):
        time.tzset()


def _strings(value: Any, path: str = "body") -> Iterator[tuple[str, str]]:
    """Every string in a request body, with a path for the failure message."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


class FakeTransport:
    """Stands in for `api.Client.request`: records every call, answers from a table.

    An unrouted path answers None, which every `Client` method already coerces to an
    empty result, so a route only has to exist when a test cares about the response.
    """

    def __init__(self, routes: dict) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.routes = dict(routes)

    def unbound(self):
        """A drop-in replacement for the unbound `Client.request` method."""

        def request(_client, method, path, body=None):
            self.calls.append((method, path, body))
            answer = self.routes.get((method, path))
            return answer(body) if callable(answer) else answer

        return request

    def bodies(self, method: str, path: str) -> list:
        return [body for verb, route, body in self.calls if (verb, route) == (method, path)]

    def touched(self) -> list[str]:
        return [f"{verb} {route}" for verb, route, _ in self.calls]

    def mutations(self) -> list[str]:
        return [
            f"{verb} {route}"
            for verb, route, _ in self.calls
            if verb in ("PUT", "DELETE")
            or (verb == "POST" and route not in _READ_ONLY_POSTS)
        ]


class CliTestCase(unittest.TestCase):
    """Credentials in a temp dir, transport faked, stdout captured."""

    routes: dict = {}

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = pathlib.Path(tmp.name) / "credentials.json"
        path.write_text(json.dumps({"access_token": "fake-token", "inbox_id": "inbox9"}))
        self.enterContext(mock.patch.object(config, "CONFIG_PATH", path))
        self.transport = FakeTransport(
            {
                ("GET", "/project"): PROJECTS,
                ("GET", "/project/p1/task/t1"): dict(TASK),
                ("GET", "/project/inbox/data"): {
                    "tasks": [{"id": "i1", "projectId": "inbox9", "title": "note"}]
                },
                ("POST", "/task"): lambda body: {**body, "id": "new1"},
                ("POST", "/task/t1"): lambda body: dict(body),
                ("POST", "/task/move"): [{"id": "t1", "etag": "e1"}],
                ("POST", "/task/completed"): list(COMPLETED),
                ("POST", "/task/filter"): [],
                **self.routes,
            }
        )
        self.enterContext(
            mock.patch.object(api.Client, "request", self.transport.unbound())
        )

    def run_cli(self, *argv: str) -> dict:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            status = cli.main(list(argv))
        text = buffer.getvalue()
        self.assertEqual(status, 0)
        # The QML contract: exactly one JSON object per invocation, always exit 0.
        self.assertEqual(text.count("\n"), 1, f"expected one JSON line, got {text!r}")
        return json.loads(text)

    def assertOffsetDates(self) -> None:
        """No date-shaped string may reach the API without a UTC offset."""
        for verb, route, body in self.transport.calls:
            for where, value in _strings(body):
                if _DATEISH.match(value):
                    self.assertRegex(
                        value, _OFFSET,
                        f"{verb} {route} sent {where}={value!r} with no UTC offset — "
                        "TickTick answers 200 and throws it away",
                    )


class WireDateTest(CliTestCase):
    def test_a_bare_due_date_is_qualified_before_it_is_sent(self):
        payload = self.run_cli("add", "review the spec", "--due", "2026-08-06", "--project", "p1")
        self.assertTrue(payload["ok"])
        body = self.transport.bodies("POST", "/task")[0]
        self.assertTrue(body["isAllDay"])
        self.assertRegex(body["dueDate"], _OFFSET)
        self.assertNotEqual(body["dueDate"], "2026-08-06")
        self.assertOffsetDates()

    def test_a_timed_due_date_keeps_its_offset(self):
        self.run_cli("add", "call the bank", "--due", "tomorrow 9am", "--project", "p1")
        body = self.transport.bodies("POST", "/task")[0]
        self.assertFalse(body["isAllDay"])
        self.assertOffsetDates()

    def test_the_completed_window_is_offset_qualified(self):
        self.run_cli("completed", "--days", "3")
        body = self.transport.bodies("POST", "/task/completed")[0]
        self.assertRegex(body["startDate"], _OFFSET)
        self.assertRegex(body["endDate"], _OFFSET)
        self.assertLess(body["startDate"], body["endDate"])
        self.assertOffsetDates()

    def test_editing_a_due_date_qualifies_it_too(self):
        self.run_cli("edit", "t1", "--project", "p1", "--due", "2026-08-09")
        body = self.transport.bodies("POST", "/task/t1")[0]
        self.assertRegex(body["dueDate"], _OFFSET)
        self.assertOffsetDates()

    def test_wire_date_refuses_a_value_with_no_offset(self):
        with mock.patch.object(cli.dates, "format", return_value="2026-08-06"):
            with self.assertRaises(UsageError):
                cli._wire_date(cli.dates.now())


class CompletedTest(CliTestCase):
    def test_rows_match_the_tasks_shape_and_are_newest_first(self):
        payload = self.run_cli("completed", "--days", "7")
        rows = payload["tasks"]
        self.assertEqual([row["id"] for row in rows], ["c2", "c1"])
        self.assertEqual({row["bucket"] for row in rows}, {"completed"})
        self.assertTrue(all(row["completedLabel"] for row in rows))
        self.assertEqual(rows[0]["project"], "Home")
        # Same keys as a `tasks` row, so the widget delegate binds without a special case.
        self.assertLessEqual(set(cli.views.row({})), set(rows[0]))

    def test_limit_truncates(self):
        self.assertEqual(len(self.run_cli("completed", "--limit", "1")["tasks"]), 1)

    def test_a_project_filter_reaches_the_endpoint(self):
        self.run_cli("completed", "--project", "p1")
        self.assertEqual(self.transport.bodies("POST", "/task/completed")[0]["projectIds"], ["p1"])


class MoveTest(CliTestCase):
    def test_edit_move_to_uses_the_move_endpoint(self):
        payload = self.run_cli("edit", "t1", "--project", "p1", "--move-to", "p2")
        self.assertTrue(payload["moved"])
        self.assertEqual(
            self.transport.bodies("POST", "/task/move"),
            [[{"fromProjectId": "p1", "toProjectId": "p2", "taskId": "t1"}]],
        )
        # Rewriting projectId through POST /task/{id} answers 200 and moves nothing.
        self.assertNotIn("POST /task/t1", self.transport.touched())

    def test_edit_writes_fields_to_the_source_project_then_moves(self):
        self.run_cli("edit", "t1", "--project", "p1", "--title", "renamed", "--move-to", "p2")
        body = self.transport.bodies("POST", "/task/t1")[0]
        self.assertEqual(body["title"], "renamed")
        self.assertEqual(body["projectId"], "p1")
        self.assertEqual(len(self.transport.bodies("POST", "/task/move")), 1)

    def test_the_move_subcommand_reports_where_it_came_from(self):
        payload = self.run_cli("move", "t1", "p2", "--project", "p1")
        self.assertEqual(payload["fromProjectId"], "p1")
        self.assertTrue(payload["moved"])
        self.assertEqual(payload["task"]["projectId"], "p2")
        self.assertEqual(payload["task"]["project"], "Home")

    def test_moving_into_the_same_project_asks_for_nothing(self):
        payload = self.run_cli("move", "t1", "p1", "--project", "p1")
        self.assertFalse(payload["moved"])
        self.assertEqual(self.transport.bodies("POST", "/task/move"), [])


class ReminderTest(unittest.TestCase):
    def test_the_trigger_table(self):
        cases = [
            ("ontime", False, "TRIGGER:PT0S"),
            ("on time", False, "TRIGGER:PT0S"),
            ("0", False, "TRIGGER:PT0S"),
            ("5m", False, "TRIGGER:-PT5M"),
            ("15m", False, "TRIGGER:-PT15M"),
            ("30m", False, "TRIGGER:-PT30M"),
            ("1h", False, "TRIGGER:-PT60M"),  # TickTick emits -PT60M, never -PT1H
            ("2h", False, "TRIGGER:-PT120M"),
            ("1d", False, "TRIGGER:-P1D"),
            ("9am", True, "TRIGGER:P0DT9H0M0S"),
            ("12pm", True, "TRIGGER:P0DT12H0M0S"),
            ("1d", True, "TRIGGER:-P0DT15H0M0S"),
            ("2d", True, "TRIGGER:-P1DT15H0M0S"),
            ("TRIGGER:-P1DT15H0M0S", False, "TRIGGER:-P1DT15H0M0S"),
        ]
        for spec, all_day, expected in cases:
            with self.subTest(spec=spec, all_day=all_day):
                self.assertEqual(cli._reminder(spec, all_day=all_day), expected)

    def test_a_wall_clock_reminder_needs_an_all_day_task(self):
        with self.assertRaises(UsageError):
            cli._reminder("9am", all_day=False)

    def test_nonsense_is_refused(self):
        for spec in ("", "soonish", "0m", "-5m", "25am"):
            with self.subTest(spec=spec), self.assertRaises(UsageError):
                cli._reminder(spec)


class ReminderGuardTest(CliTestCase):
    def test_a_reminder_without_a_date_is_refused_before_any_write(self):
        payload = self.run_cli("add", "buy milk", "--remind", "30m", "--project", "p1")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "usage")
        # The trap: TickTick would accept this and drop the reminder on the floor.
        self.assertEqual(self.transport.mutations(), [])

    def test_a_dated_task_sends_the_reminder(self):
        self.run_cli("add", "standup", "--due", "tomorrow 9am", "--remind", "15m", "--project", "p1")
        self.assertEqual(self.transport.bodies("POST", "/task")[0]["reminders"], ["TRIGGER:-PT15M"])


class InboxProbeTest(CliTestCase):
    def test_the_probe_reads_the_inbox_and_never_writes(self):
        inbox_id, error = auth.probe_inbox(api.Client("fake-token"))
        self.assertEqual((inbox_id, error), ("inbox9", ""))
        self.assertEqual(self.transport.mutations(), [])


class InboxProbeFallbackTest(CliTestCase):
    routes = {
        ("GET", "/project/inbox/data"): {"tasks": []},
        ("POST", "/task/filter"): [
            {"id": "a", "projectId": "p1"},
            {"id": "b", "projectId": "inbox42"},
        ],
    }

    def test_the_probe_falls_back_to_the_undone_list(self):
        self.assertEqual(auth.probe_inbox(api.Client("fake-token")), ("inbox42", ""))
        self.assertEqual(self.transport.mutations(), [])


class InboxProbeEmptyTest(CliTestCase):
    routes = {("GET", "/project/inbox/data"): {"tasks": []}}

    def test_an_empty_inbox_stores_nothing_and_writes_nothing(self):
        inbox_id, error = auth.probe_inbox(api.Client("fake-token"))
        self.assertIsNone(inbox_id)
        self.assertTrue(error)
        self.assertEqual(self.transport.mutations(), [])


if __name__ == "__main__":
    unittest.main()
