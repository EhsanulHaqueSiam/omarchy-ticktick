"""The features test_e2e.py does not reach: tags, subtasks, start dates, borrowed
credentials, the text renderer, and project groups.

Anything that needs an account runs against the same fake server and the same temp
credential/state paths as the end-to-end suite, by borrowing its `Base`. The rest is
pure: `views.row` and `render` never touch the network at all.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tests.fake_ticktick import TOKEN  # noqa: E402
from tests.test_e2e import Base  # noqa: E402
from ticktick import config, render, views  # noqa: E402

#: A TickTick timestamp ends in a four-digit UTC offset; one that does not is thrown
#: away server-side after a 200 OK.
_OFFSET = re.compile(r"[+-]\d{4}$")


# ---------------------------------------------------------------- tags


class TagTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.login()
        self.task = self.fake.add_task(title="call plumber", projectId="p-work",
                                       tags=["urgent"])
        self.fake.add_task(title="water plants", projectId="p-home", tags=["errand"])
        self.fake.add_task(title="no tags here", projectId="p-home")
        self.ok("tasks", "--view", "all", "--include-undated", "--refresh")

    def stored_tags(self) -> list[str]:
        return self.fake.tasks[self.task["id"]]["tags"]

    def test_add_echoes_the_tags_it_attached(self):
        payload = self.ok("add", "call the vet", "--tag", "#Errand")
        self.assertEqual(payload["task"]["tags"], ["errand"])

    def test_edit_tag_replaces_the_whole_list_in_the_order_given(self):
        self.ok("edit", self.task["id"], "--tag", "Home", "--tag", "#errand")
        self.assertEqual(self.stored_tags(), ["home", "errand"])

    def test_add_tag_appends_and_skips_what_is_already_there(self):
        self.ok("edit", self.task["id"], "--add-tag", "#ERRAND", "--add-tag", "urgent")
        self.assertEqual(self.stored_tags(), ["urgent", "errand"])

    def test_remove_tag_drops_one_and_leaves_the_others_in_order(self):
        self.ok("edit", self.task["id"], "--tag", "one", "--tag", "two", "--tag", "three")
        self.ok("edit", self.task["id"], "--remove-tag", "TWO")
        self.assertEqual(self.stored_tags(), ["one", "three"])

    def test_tag_filtering_is_any_of_not_all_of(self):
        view = ("tasks", "--view", "all", "--include-undated")
        self.assertEqual(self.titles(*view, "--tag", "urgent"), ["call plumber"])
        self.assertEqual(self.titles(*view, "--tag", "urgent", "--tag", "#Errand"),
                         ["call plumber", "water plants"])


# ------------------------------------------------------------- subtasks


class SubtaskTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.login()
        self.parent = self.fake.add_task(title="plan the trip", projectId="p-work")
        self.ok("tasks", "--view", "all", "--include-undated", "--refresh")

    def test_add_parent_files_the_new_task_under_it(self):
        payload = self.ok("add", "book flights", "--parent", self.parent["id"])
        created = next(t for t in self.fake.tasks.values() if t["title"] == "book flights")
        self.assertEqual(created["parentId"], self.parent["id"])
        self.assertEqual(payload["task"]["parentId"], self.parent["id"])

    def test_edit_parent_with_an_empty_value_detaches_the_subtask(self):
        child = self.fake.add_task(title="book flights", projectId="p-work",
                                   parentId=self.parent["id"])
        self.ok("tasks", "--view", "all", "--include-undated", "--refresh")
        self.ok("edit", child["id"], "--parent", "")
        self.assertEqual(self.fake.tasks[child["id"]]["parentId"], "")


# ---------------------------------------------------------- start dates


class StartDateTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.login()
        self.task = self.fake.add_task(title="sprint", projectId="p-work")
        self.ok("tasks", "--view", "all", "--include-undated", "--refresh")

    def test_add_start_reaches_the_server_with_a_utc_offset(self):
        # A bare date is accepted with 200 and silently discarded, so the task would come
        # back with no start at all and nobody would find out.
        self.ok("add", "conference", "--start", "2027-03-01", "--due", "2027-03-05")
        created = next(t for t in self.fake.tasks.values() if t["title"] == "conference")
        self.assertRegex(created["startDate"], _OFFSET)

    def test_edit_start_sets_a_start_date_on_an_existing_task(self):
        self.ok("edit", self.task["id"], "--start", "2027-03-01")
        self.assertRegex(self.fake.tasks[self.task["id"]]["startDate"], _OFFSET)


class RowShapeTest(unittest.TestCase):
    """`views.row` carries the fields the published documentation leaves out."""

    def test_tags_start_and_parent_reach_the_row(self):
        start = datetime(2027, 3, 1, 9, tzinfo=timezone.utc)
        item = views.row(
            {
                "id": "t1",
                "projectId": "p1",
                "title": "book flights",
                "parentId": "t0",
                "tags": ["urgent", "errand"],
                "startDate": "2027-03-01T09:00:00.000+0000",
            }
        )
        self.assertEqual(item["tags"], ["urgent", "errand"])
        self.assertEqual(item["parentId"], "t0")
        self.assertEqual(item["start"], int(start.timestamp()))
        self.assertTrue(item["startLabel"])

    def test_they_are_present_and_typed_on_a_task_carrying_none_of_them(self):
        # QML renders a missing key as the literal string 'undefined'.
        item = views.row({})
        self.assertEqual(item["tags"], [])
        self.assertEqual(item["parentId"], "")
        self.assertEqual(item["start"], 0)
        self.assertEqual(item["startLabel"], "")


# ------------------------------------------------- borrowed credentials


class AdoptionTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.external = self.tmp / "ticktick-cli" / "config.json"
        self.external.parent.mkdir(parents=True, exist_ok=True)
        self.external.write_text(json.dumps({"access_token": TOKEN}), encoding="utf-8")
        # EXTERNAL_TOKEN_PATHS is computed at import from XDG_CONFIG_HOME, so pointing
        # the environment at the temp dir now would be too late.
        self._patch(config, "EXTERNAL_TOKEN_PATHS", (self.external,))

    def test_a_token_another_ticktick_tool_stored_signs_us_in(self):
        self.fake.add_task(title="borrowed", projectId="p-work")
        cfg = config.load()
        self.assertEqual(cfg["access_token"], TOKEN)
        self.assertEqual(cfg["auth_method"], "adopted")
        self.assertEqual(cfg["auth_source"], str(self.external))
        self.assertIn("borrowed",
                      self.titles("tasks", "--view", "all", "--include-undated", "--refresh"))

    def test_logout_stops_the_borrowing_and_leaves_the_other_tool_alone(self):
        self.ok("logout")
        cfg = config.load()
        self.assertIs(cfg["adopt_external"], False)
        self.assertNotIn("access_token", cfg)
        self.assertIs(self.ok("status")["authed"], False)
        self.assertIn(TOKEN, self.external.read_text(encoding="utf-8"),
                      "the other tool's file is read, never written")

    def test_an_explicit_login_clears_the_flag_logout_set(self):
        self.ok("logout")
        self.login()
        self.assertNotIn("adopt_external", config.load())

    def test_signing_in_over_a_borrowed_token_actually_stores_the_new_one(self):
        # The marker that stops a borrowed token being written down must not also
        # discard one the user deliberately signed in with — that made `login` and
        # `auth` report success and change nothing at all.
        own = "own-token-9876543210abcdef"
        config.save({**config.load(), "access_token": own, "auth_method": "token"})
        stored = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(stored.get("access_token"), own,
                         "the credential file kept the borrowed token instead of the new one")
        self.assertEqual(stored.get("auth_method"), "token")

    def test_an_incidental_save_still_does_not_write_down_the_borrowed_token(self):
        # The other half of the same rule: saving something unrelated — the Inbox id —
        # must leave the borrowing a live mirror rather than making a permanent copy.
        cfg = config.load()
        self.assertEqual(cfg["access_token"], TOKEN)
        cfg["inbox_id"] = "inbox777"
        config.save(cfg)
        stored = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(stored.get("inbox_id"), "inbox777")
        self.assertNotIn("access_token", stored)


# -------------------------------------------------------------- render


class RenderTest(Base):
    """A formatting bug must never turn a successful command into a failed one."""

    def setUp(self) -> None:
        super().setUp()
        self.login()
        late = self.fake.add_task(
            title="late thing", projectId="p-work", priority=5, tags=["urgent"],
            dueDate="2020-01-01T09:00:00.000+0000",
            items=[{"id": "i1", "title": "step", "status": 0}],
        )
        doomed = self.fake.add_task(title="doomed", projectId="p-home")
        self.cases = [
            ("tasks", self.ok("tasks", "--view", "all", "--include-undated", "--refresh")),
            ("projects", self.ok("projects")),
            ("tags", self.ok("tags")),
            ("status", self.ok("status")),
            ("sync", self.ok("sync")),
            ("parse", self.ok("parse", "submit report tomorrow 5pm !high #work")),
            ("add", self.ok("add", "buy milk tomorrow")),
            ("item", self.ok("item", "add", late["id"], "another step")),
            ("complete", self.ok("complete", late["id"])),
            ("delete", self.ok("delete", doomed["id"], "--yes")),
            ("edit", self.fails("edit", "nope")),
        ]

    def test_every_payload_the_cli_emits_renders_to_something(self):
        for command, payload in self.cases:
            with self.subTest(command=command):
                self.assertTrue(render.render(command, payload, width=80).strip())

    def test_colour_off_leaves_no_escape_sequence_behind(self):
        for command, payload in self.cases:
            with self.subTest(command=command):
                text = render.render(command, payload, color=False, width=80)
                self.assertNotIn("\033", text)


class RenderFallbackTest(unittest.TestCase):
    def test_a_payload_the_formatter_chokes_on_falls_back_to_json(self):
        text = render.render("tasks", {"tasks": [{"priority": "not a number"}]}, width=80)
        self.assertIn("not a number", text)

    def test_an_unknown_command_still_prints_its_payload(self):
        self.assertIn("surprise", render.render("no-such-command", {"surprise": 1}, width=80))

    def test_visible_length_ignores_ansi_escapes(self):
        self.assertEqual(render._visible("plain"), 5)
        self.assertEqual(render._visible(render.RED + "plain" + render.RESET), 5)
        self.assertEqual(render._visible(render.BOLD + render.DIM + "ab"), 2)


# ------------------------------------------------------ project groups


class ProjectGroupTest(Base):
    def test_project_groups_lists_the_folders_projects_sit_in(self):
        self.login()
        self.fake.groups = [{"id": "g1", "name": "Personal", "sortOrder": 2}]
        payload = self.ok("project", "groups")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["groups"], [{"id": "g1", "name": "Personal", "sortOrder": 2}])


if __name__ == "__main__":
    unittest.main()
