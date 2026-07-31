"""End-to-end: TickTick's own rules — smart lists, list grouping, and notes.

Same harness as `test_e2e`: the real CLI over real HTTP against the fake account.
What is asserted here is behaviour a user would describe in TickTick's own words —
"Today shows what is due today and what is late", "notes are not tasks", "the
headings are my list names" — rather than the shape of any one function.
"""

from __future__ import annotations

import pathlib
import sys
import time
import unittest
from datetime import timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tests.fake_ticktick import TOKEN  # noqa: E402
from tests.test_e2e import Base  # noqa: E402
from ticktick import api, dates, store  # noqa: E402


def stamp(days: float = 0, hour: int | None = None) -> str:
    """A TickTick-format timestamp `days` from now, optionally at a fixed hour."""
    when = dates.now() + timedelta(days=days)
    if hour is not None:
        when = when.replace(hour=hour, minute=0, second=0, microsecond=0)
    return dates.format(when)


class Populated(Base):
    """One task per bucket, spread across the Inbox and two lists."""

    def setUp(self) -> None:
        super().setUp()
        self.login()
        self.fake.add_task(id="t-late", title="renew passport", projectId=self.fake.inbox_id,
                           dueDate=stamp(-2))
        self.fake.add_task(id="t-today", title="call the bank", projectId=self.fake.inbox_id,
                           dueDate=stamp(0, hour=23))
        self.fake.add_task(id="t-work", title="submit lab report", projectId="p-work",
                           dueDate=stamp(0, hour=22))
        self.fake.add_task(id="t-soon", title="read chapter 4", projectId="p-work",
                           dueDate=stamp(1, hour=21))
        self.fake.add_task(id="t-far", title="book flights", projectId="p-home",
                           dueDate=stamp(20, hour=12))
        self.fake.add_task(id="t-none", title="someday idea", projectId="p-home")
        self.fake.add_task(id="t-note", title="meeting notes", projectId="p-work",
                           kind="NOTE", content="what was said", dueDate=stamp(0, hour=20))

    def ids(self, *argv: str) -> list[str]:
        return [t["id"] for t in self.ok(*argv)["tasks"]]


# ------------------------------------------------------------------ notes


class NoteTest(Populated):
    def test_a_note_is_carried_but_never_counted(self):
        payload = self.ok("tasks", "--refresh", "--view", "all", "--include-undated")
        rows = {t["id"]: t for t in payload["tasks"]}
        self.assertIn("t-note", rows, "a note still belongs in the list")
        self.assertIs(rows["t-note"]["isNote"], True)
        self.assertEqual(rows["t-note"]["kind"], "NOTE")
        self.assertIs(rows["t-today"]["isNote"], False)

        counts = payload["counts"]
        self.assertEqual(counts["notes"], 1)
        self.assertEqual(counts["total"], 6, "six things to do, plus one note")
        # The note is due today; the badge must not include it.
        self.assertEqual(counts["today"], 2, "call the bank + submit lab report")
        self.assertEqual(counts["overdue"], 1)

    def test_a_note_does_not_inflate_its_list_count(self):
        payload = self.ok("tasks", "--refresh", "--view", "all", "--include-undated")
        counts = {p["name"]: p["count"] for p in payload["projects"]}
        # p-work holds t-work, t-soon and the note; only the two tasks count.
        self.assertEqual(counts["Work"], 2)

    def test_a_task_with_no_kind_field_reads_as_a_todo(self):
        # The API omits `kind` for ordinary tasks, so absent must not mean unknown.
        self.fake.tasks["t-today"].pop("kind", None)
        rows = {t["id"]: t for t in self.ok("tasks", "--refresh", "--view", "all")["tasks"]}
        self.assertEqual(rows["t-today"]["kind"], "TEXT")
        self.assertIs(rows["t-today"]["isNote"], False)


# ------------------------------------------------------- the smart lists


class SmartListTest(Populated):
    def test_today_is_due_today_plus_whatever_is_late(self):
        self.assertEqual(
            sorted(self.ids("tasks", "--refresh", "--view", "today")),
            ["t-late", "t-note", "t-today", "t-work"],
        )

    def test_tomorrow_is_tomorrow_alone(self):
        self.assertEqual(self.ids("tasks", "--refresh", "--view", "tomorrow"), ["t-soon"])

    def test_next_reaches_a_week_ahead_and_no_further(self):
        got = sorted(self.ids("tasks", "--refresh", "--view", "next"))
        self.assertIn("t-soon", got)
        self.assertNotIn("t-far", got, "twenty days out is not the next seven")
        self.assertNotIn("t-none", got, "a date view has nothing to say about an undated task")

    def test_overdue_is_only_what_slipped(self):
        self.assertEqual(self.ids("tasks", "--refresh", "--view", "overdue"), ["t-late"])

    def test_inbox_is_a_list_not_a_date_window(self):
        # Everything filed in the Inbox, whatever its date — including undated,
        # which is most of what an Inbox holds.
        self.assertEqual(
            sorted(self.ids("tasks", "--refresh", "--view", "inbox")),
            ["t-late", "t-today"],
        )

    def test_a_list_shows_its_undated_tasks_without_being_asked(self):
        self.assertIn("t-none", self.ids("tasks", "--refresh", "--view", "project",
                                         "--project", "p-home"))

    def test_the_badge_does_not_change_meaning_when_you_browse_into_a_list(self):
        whole = self.ok("tasks", "--refresh", "--view", "all")["counts"]
        for argv in (("--view", "project", "--project", "p-home"),
                     ("--view", "inbox",),
                     ("--view", "tomorrow",)):
            with self.subTest(argv=argv):
                self.assertEqual(self.ok("tasks", *argv)["counts"], whole)

    def test_a_view_the_helper_does_not_know_is_a_usage_error_not_a_crash(self):
        payload = self.run_cli("tasks", "--view", "yesterday")
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["error"], "usage")

    def test_an_empty_inbox_shows_nothing_rather_than_the_whole_account(self):
        # There is no Inbox id to be found when no task lives there, and treating an
        # empty scope as "no filter" put every task the user owns under "Inbox".
        for tid in ("t-late", "t-today"):
            self.fake.tasks[tid]["projectId"] = "p-home"
        self.assertEqual(self.ids("tasks", "--refresh", "--view", "inbox"), [])
        self.assertTrue(self.ids("tasks", "--view", "all"), "other views still work")

    def test_lists_the_catalogue_does_not_know_still_group_separately(self):
        # Every unknown list ranks the same, so without the id as a tiebreaker their
        # rows interleave — and the UI, which heads a section wherever the id changes,
        # heads the same list twice.
        for tid, pid in (("t-today", "p-ghost-a"), ("t-work", "p-ghost-b"),
                         ("t-soon", "p-ghost-a"), ("t-far", "p-ghost-b")):
            self.fake.tasks[tid]["projectId"] = pid
        rows = self.ok("tasks", "--refresh", "--view", "all", "--include-undated",
                       "--sort", "list")["tasks"]
        runs = []
        for row in rows:
            if not runs or runs[-1] != row["projectId"]:
                runs.append(row["projectId"])
        self.assertEqual(len(runs), len(set(runs)), f"a list was split in two: {runs}")


# ------------------------------------------------------------- grouping


class SortTest(Populated):
    def test_sorting_by_list_groups_each_list_together(self):
        rows = self.ok("tasks", "--refresh", "--view", "all", "--include-undated",
                       "--sort", "list")["tasks"]
        seen: list[str] = []
        for row in rows:
            if not seen or seen[-1] != row["projectId"]:
                seen.append(row["projectId"])
        self.assertEqual(len(seen), len(set(seen)), f"a list was split in two: {seen}")
        self.assertEqual(seen[0], self.fake.inbox_id, "the Inbox reads first, as in TickTick")

    def test_sorting_by_time_puts_what_is_late_first(self):
        rows = self.ok("tasks", "--refresh", "--view", "all", "--include-undated",
                       "--sort", "time")["tasks"]
        self.assertEqual(rows[0]["id"], "t-late")
        buckets = [r["bucket"] for r in rows]
        self.assertEqual(buckets, sorted(buckets, key=dates.BUCKET_ORDER.get))

    def test_sorting_by_priority_puts_the_urgent_first(self):
        self.fake.tasks["t-far"]["priority"] = 5
        rows = self.ok("tasks", "--refresh", "--view", "all", "--include-undated",
                       "--sort", "priority")["tasks"]
        self.assertEqual(rows[0]["id"], "t-far")

    def test_sorting_by_title_is_alphabetical(self):
        titles = [t["title"] for t in self.ok("tasks", "--refresh", "--view", "all",
                                              "--include-undated", "--sort", "title")["tasks"]]
        self.assertEqual(titles, sorted(titles, key=str.lower))

    def test_the_sort_travels_with_the_payload_so_the_ui_can_head_the_groups(self):
        self.assertEqual(self.ok("tasks", "--refresh", "--sort", "list")["sort"], "list")
        self.assertEqual(self.ok("tasks", "--sort", "time")["sort"], "time")


class FolderTest(Populated):
    def test_lists_carry_their_folder_and_the_folders_are_named(self):
        payload = self.ok("tasks", "--refresh", "--view", "all")
        by_id = {p["id"]: p for p in payload["projects"]}
        self.assertEqual(by_id["p-work"]["groupId"], "g-study")
        self.assertEqual(by_id["p-home"]["groupId"], "", "a loose list has no folder")
        self.assertEqual([g["name"] for g in payload["groups"]], ["University work"])

    def test_folders_survive_an_endpoint_that_stops_answering(self):
        # Undocumented endpoint: losing it must cost the folder headings, nothing else.
        self.ok("tasks", "--refresh", "--view", "all")
        self.fake.groups = []
        payload = self.ok("tasks", "--refresh", "--view", "all")
        self.assertEqual(payload["groups"], [])
        self.assertTrue(payload["tasks"], "the task list is unaffected")


# ------------------------------------------------------- editing a task


class AddTest(Populated):
    def test_a_task_added_from_today_lands_on_today(self):
        # TickTick's rule, and the reason the row appears where it was typed instead
        # of in an undated list nobody is looking at.
        row = self.ok("add", "water the plants", "--due-default", "today")["task"]
        self.assertEqual(row["bucket"], "today")

    def test_the_default_never_overrides_a_date_the_user_named(self):
        for text, extra in (("water the plants tomorrow", []),
                            ("water the plants", ["--due", "tomorrow"])):
            with self.subTest(text=text):
                row = self.ok("add", text, "--due-default", "today", *extra)["task"]
                self.assertEqual(row["bucket"], "tomorrow")

    def test_a_default_that_is_not_a_date_is_a_usage_error(self):
        payload = self.run_cli("add", "something", "--due-default", "whenever")
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["error"], "usage")

    def test_no_default_leaves_a_task_undated(self):
        self.assertEqual(self.ok("add", "someday maybe")["task"]["bucket"], "undated")


class EditTest(Populated):
    def test_a_reminder_can_be_set_and_cleared_after_the_fact(self):
        self.ok("edit", "t-today", "--project", self.fake.inbox_id, "--remind", "30m")
        self.assertEqual(self.fake.tasks["t-today"]["reminders"], ["TRIGGER:-PT30M"])
        self.ok("edit", "t-today", "--project", self.fake.inbox_id, "--clear-remind")
        self.assertEqual(self.fake.tasks["t-today"]["reminders"], [])

    def test_a_reminder_without_a_due_date_is_refused_rather_than_dropped(self):
        # TickTick accepts one and silently forgets it, which is worse than an error.
        payload = self.run_cli("edit", "t-none", "--project", "p-home", "--remind", "1h")
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["error"], "usage")

    def test_repeating_can_be_turned_on_and_off(self):
        self.ok("edit", "t-today", "--project", self.fake.inbox_id, "--repeat", "every 2 weeks")
        self.assertEqual(self.fake.tasks["t-today"]["repeatFlag"],
                         "RRULE:FREQ=WEEKLY;INTERVAL=2")
        self.ok("edit", "t-today", "--project", self.fake.inbox_id, "--clear-repeat")
        self.assertIn(self.fake.tasks["t-today"].get("repeatFlag"), (None, ""))

    def test_contradictory_flags_are_refused(self):
        for argv in (("--remind", "1h", "--clear-remind"),
                     ("--repeat", "daily", "--clear-repeat")):
            with self.subTest(argv=argv):
                payload = self.run_cli("edit", "t-today", "--project", self.fake.inbox_id, *argv)
                self.assertIs(payload["ok"], False)
                self.assertEqual(payload["error"], "usage")

    def test_the_start_date_round_trips_through_the_row(self):
        self.ok("edit", "t-today", "--project", self.fake.inbox_id, "--start", "2026-08-15 09:00")
        row = self.ok("task", "t-today", "--project", self.fake.inbox_id)["task"]
        self.assertTrue(row["startIso"].startswith("2026-08-15T09:00"),
                        f"an editor seeds its field from this: {row['startIso']!r}")


# --------------------------------------------------- the durable outbox


class OutboxTest(Populated):
    def warm(self) -> None:
        self.ok("tasks", "--refresh", "--view", "all", "--include-undated")

    def test_a_captive_portal_never_eats_the_queue(self):
        # A proxy login page is a 200 that is not JSON: no verdict on the request,
        # so nothing may be thrown away however many times it happens.
        self.warm()
        self.ok("complete", "t-today", "--project", self.fake.inbox_id, "--offline")
        self.assertEqual(store.pending(store.load()), 1)

        self.fake.garbage_next = 10_000
        for _ in range(12):
            self.run_cli("tasks", "--view", "all", "--include-undated")
        self.assertEqual(store.pending(store.load()), 1,
                         "a completion the user watched happen was silently discarded")

        self.fake.garbage_next = 0
        self.ok("sync")
        self.assertEqual(store.pending(store.load()), 0)
        self.assertEqual(self.fake.tasks["t-today"]["status"], 2)

    def test_a_write_the_server_refuses_is_reported_not_swallowed(self):
        self.warm()
        payload = self.ok("add", "ghost task", "--project", "p-does-not-exist")
        self.assertTrue(payload.get("warning"), f"a refused create reported success: {payload}")

    def test_work_the_server_accepted_is_not_sent_twice_after_a_later_failure(self):
        # The flush at the top of a command has already reached the server; a command
        # that then raises must not leave that work queued for a second delivery.
        self.warm()
        self.ok("add", "buy milk", "--offline")
        self.assertEqual(store.pending(store.load()), 1)

        self.fails("complete", "not-a-real-task-id")
        self.assertEqual(store.pending(store.load()), 0,
                         "the create landed upstream but stayed queued")

        self.ok("sync")
        titles = [t["title"] for t in self.fake.tasks.values()]
        self.assertEqual(titles.count("buy milk"), 1, "the task was created twice")

    def test_a_queued_edit_does_not_revert_what_changed_elsewhere(self):
        # No PATCH: the body replaces the task. A stale snapshot would put every
        # other field back the way it was when the command ran.
        self.warm()
        self.fake.fail_next = 99
        self.run_cli("edit", "t-work", "--project", "p-work", "--title", "report v2")
        self.assertEqual(store.pending(store.load()), 1)
        self.fake.fail_next = 0

        # Another device moves the due date while our edit waits in the queue.
        moved = stamp(9, hour=12)
        self.fake.tasks["t-work"]["dueDate"] = moved
        self.ok("tasks", "--refresh", "--view", "all", "--include-undated")
        self.ok("sync")

        self.assertEqual(self.fake.tasks["t-work"]["title"], "report v2")
        self.assertEqual(self.fake.tasks["t-work"]["dueDate"], moved,
                         "the title-only edit reverted the due date")

    def test_a_subtask_created_offline_finds_its_parent(self):
        self.warm()
        parent = self.ok("add", "parent task", "--offline")["task"]["id"]
        self.assertTrue(store.is_local_id(parent))
        self.ok("add", "child task", "--offline", "--parent", parent)
        self.ok("sync")

        child = next(t for t in self.fake.tasks.values() if t["title"] == "child task")
        real = next(t for t in self.fake.tasks.values() if t["title"] == "parent task")
        self.assertEqual(child.get("parentId"), real["id"],
                         "the child shipped a local- id no TickTick client can resolve")

    def test_a_create_lands_in_the_cache_even_when_the_cache_was_empty(self):
        # `state["tasks"] or []` bound a throwaway list on an empty cache, so the
        # server's copy was dropped and the next queued op addressed a task that,
        # as far as the cache was concerned, had no project.
        self.ok("logout")
        self.login()
        self.assertEqual(store.load()["tasks"], [])
        created = self.ok("add", "draft thing", "--offline")["task"]["id"]
        self.ok("complete", created, "--offline")
        self.ok("sync")

        self.assertEqual(store.pending(store.load()), 0)
        landed = next(t for t in self.fake.tasks.values() if t["title"] == "draft thing")
        self.assertEqual(landed["status"], 2, "the completion never reached the real task")

    def test_a_lapsed_token_queues_the_write_instead_of_discarding_it(self):
        # Signed in, then the credential expired. The change is already on the user's
        # screen; abandoning it before it is written down is the one thing the outbox
        # exists to prevent.
        self.warm()
        from ticktick import config
        config.save({**config.load(), "expires_at": 1.0})

        payload = self.ok("add", "milk")
        self.assertEqual(payload.get("warning"), "not signed in")
        self.assertEqual(store.pending(store.load()), 1)
        self.ok("complete", "t-today", "--project", self.fake.inbox_id)
        self.assertEqual(store.pending(store.load()), 2)

        self.login()  # a fresh sign-in drains everything that was waiting
        self.ok("sync")
        self.assertEqual(store.pending(store.load()), 0)
        self.assertIn("milk", [t["title"] for t in self.fake.tasks.values()])
        self.assertEqual(self.fake.tasks["t-today"]["status"], 2)

    def test_never_signed_in_is_still_an_auth_error_not_a_silent_queue(self):
        # No account for a queue to drain into, so "you are not signed in" is the only
        # useful answer — and CI asserts exactly this contract.
        self.ok("logout")
        for argv in (("add", "hello"), ("complete", "abc"), ("delete", "abc"),
                     ("edit", "abc", "--title", "y")):
            with self.subTest(argv=argv):
                self.assertEqual(self.fails(*argv)["error"], "auth")
        self.assertEqual(store.pending(store.load()), 0)

    def test_a_backoff_the_file_claims_is_capped_on_the_way_in(self):
        # An absolute stamp on a clock that can jump. Capping only what `blocked_for`
        # *reports* left the gate inside `flush` — which reads the same stamp — shut
        # for however long the file said, which could be years.
        self.warm()
        state = store.load()
        state["retry_after"] = 9e9  # a clock jump, or a hand-edited file
        store.save(state)

        reloaded = store.load()
        self.assertLessEqual(reloaded["retry_after"], time.time() + store.MAX_BACKOFF + 1,
                             "the stamp itself must be capped, not merely reported capped")
        self.assertLessEqual(store.blocked_for(reloaded), store.MAX_BACKOFF)
        self.assertEqual(store.blocked_for(reloaded, now=time.time() + store.MAX_BACKOFF + 1), 0,
                         "the backoff must lift on its own within the cap")

    def test_an_entry_merged_in_from_another_process_is_not_sent_twice(self):
        # Two processes touch this file routinely — the widget polls on a timer while
        # the user runs commands in a terminal. `save` merges in whatever the other one
        # queued, but an entry we never saw at *load* time stayed "unknown" for the
        # rest of our life, so the next save merged it back in after we had synced and
        # dropped it. Creates are not idempotent.
        self.warm()
        client = api.Client(TOKEN, base=self.server.base)

        ours = store.load()                       # the long-lived process
        theirs = store.load()                     # a terminal command, concurrently
        store.enqueue(theirs, {"op": "create", "taskId": store.new_local_id(),
                               "projectId": "p-work", "task": {"title": "from elsewhere"}})
        store.save(theirs)

        store.save(ours)                          # merges their entry into ours
        self.assertEqual(store.pending(ours), 1)

        store.flush(ours, client)                 # …and we send it
        self.assertEqual(store.pending(ours), 0)
        store.save(ours)

        self.assertEqual(store.pending(store.load()), 0, "the entry came back after syncing")
        self.ok("sync")
        titles = [t["title"] for t in self.fake.tasks.values()]
        self.assertEqual(titles.count("from elsewhere"), 1, "the task was created twice")

    def test_add_echoes_the_task_it_created_not_one_with_the_same_name(self):
        self.warm()
        self.fake.add_task(id="t-standup", title="Standup", projectId="p-work")
        self.ok("tasks", "--refresh", "--view", "all", "--include-undated")
        echoed = self.ok("add", "Standup")["task"]["id"]
        self.assertNotEqual(echoed, "t-standup")
        self.assertEqual(self.fake.tasks[echoed]["title"], "Standup")


if __name__ == "__main__":
    unittest.main()
