"""End-to-end: the real CLI, over real HTTP, against a fake TickTick.

Each test runs `cli.main(argv)` exactly as `bin/ticktick` does and parses the one
JSON object it prints. Nothing is stubbed below argv except the API's far side and
the two paths that would otherwise touch the developer's own account:

* `config.CONFIG_PATH` -> a temp file, so a test can never read or clobber real
  credentials, and
* `store.STATE_PATH`   -> a temp file, so the cache and outbox start empty.

Retry delays are shrunk to milliseconds; leaving them at their real values would
make the throttle tests take half a minute for no extra coverage.
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tests.fake_ticktick import Server, TOKEN  # noqa: E402
from ticktick import api, cli, config, store  # noqa: E402


class Base(unittest.TestCase):
    """Boots a fake server and redirects every persistent path into a temp dir."""

    def setUp(self) -> None:
        self.server = Server()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.fake = self.server.fake

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)

        self._patch(api.Client, "BASE", self.server.base)
        self._patch(config, "CONFIG_PATH", self.tmp / "credentials.json")
        self._patch(store, "STATE_PATH", self.tmp / "state.json")
        # Real backoff is seconds; the tests only care that it happens.
        self._patch(api, "_RETRY_DELAYS", (0.001, 0.001))
        self._patch(api, "_THROTTLE_DELAYS", (0.001, 0.001, 0.001))

    def _patch(self, target, name, value) -> None:
        old = getattr(target, name)
        setattr(target, name, value)
        self.addCleanup(setattr, target, name, old)

    # -- driving the CLI -------------------------------------------------

    def run_cli(self, *argv: str, stdin: str | None = None) -> dict:
        """Run one subcommand and return its single JSON object."""
        out, err = io.StringIO(), io.StringIO()
        old_stdin = sys.stdin
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(list(argv))
        finally:
            sys.stdin = old_stdin
        self.assertEqual(code, 0, f"{argv} exited {code}; stderr={err.getvalue()!r}")
        text = out.getvalue().strip()
        self.assertTrue(text, f"{argv} printed nothing; stderr={err.getvalue()!r}")
        self.assertEqual(len(text.splitlines()), 1,
                         f"{argv} printed more than one line:\n{text}")
        return json.loads(text)

    def ok(self, *argv: str, stdin: str | None = None) -> dict:
        payload = self.run_cli(*argv, stdin=stdin)
        self.assertIs(payload.get("ok"), True, f"{argv} failed: {payload}")
        return payload

    def fails(self, *argv: str, stdin: str | None = None) -> dict:
        payload = self.run_cli(*argv, stdin=stdin)
        self.assertIs(payload.get("ok"), False, f"{argv} unexpectedly succeeded: {payload}")
        return payload

    def login(self) -> None:
        self.ok("login", stdin=TOKEN)

    def titles(self, *argv: str) -> list[str]:
        return [t["title"] for t in self.ok(*argv)["tasks"]]


# ---------------------------------------------------------------- auth


class AuthTest(Base):
    def test_every_command_answers_json_when_signed_out(self):
        # A traceback on stdout would blank the widget; the contract is that
        # failure is data.
        for argv in (("tasks",), ("projects",), ("status",), ("sync",),
                     ("complete", "x"), ("delete", "x"), ("add", "hello"),
                     ("edit", "x", "--title", "y"), ("check", "x", "i"),
                     ("item", "add", "x", "step"), ("tags",), ("search", "x")):
            with self.subTest(argv=argv):
                payload = self.run_cli(*argv)
                self.assertIn("ok", payload)
                if payload["ok"] is False:
                    self.assertIn(payload["error"],
                                  {"auth", "network", "api", "usage", "notfound"})

    def test_login_stores_a_verified_token(self):
        payload = self.ok("login", stdin=TOKEN)
        self.assertTrue(payload["authed"])
        self.assertEqual(payload["method"], "token")
        self.assertEqual(config.load()["access_token"], TOKEN)

    def test_login_reads_the_token_from_stdin_not_argv(self):
        # argv is world-readable through /proc; stdin is not. The stdin path must work
        # on its own, with no --token anywhere.
        self.ok("login", stdin=f"  {TOKEN}\n")
        self.assertEqual(config.load()["access_token"], TOKEN)

    def test_login_rejects_a_token_the_server_refuses(self):
        payload = self.fails("login", "--token", "wrong-token-here-1234")
        self.assertEqual(payload["error"], "auth")
        self.assertNotIn("access_token", config.load())

    def test_login_rejects_an_obviously_malformed_paste(self):
        for bad in ("short", "Bearer abcdefghijklmnop", "  "):
            with self.subTest(bad=bad):
                self.assertEqual(self.fails("login", "--token", bad)["error"], "usage")

    def test_login_clears_a_stale_oauth_expiry(self):
        # An `expires_at` left by an old OAuth sign-in would make an otherwise good
        # token read as expired and lock the user out of their own account.
        config.save({"expires_at": 1.0})
        self.ok("login", stdin=TOKEN)
        self.assertNotIn("expires_at", config.load())
        self.assertIs(self.ok("status")["authed"], True)

    def test_logout_forgets_the_token_and_the_cached_titles(self):
        self.login()
        self.fake.add_task(title="private thing", projectId="p-work")
        self.ok("tasks", "--view", "all", "--include-undated")
        self.assertTrue(store.STATE_PATH.exists())

        self.ok("logout")
        self.assertNotIn("access_token", config.load())
        self.assertFalse(store.STATE_PATH.exists(),
                         "signing out must take the cached task titles with it")

    def test_a_rejected_token_reports_auth_not_a_crash(self):
        self.login()
        self.fake.auth_fails = True
        self.assertEqual(self.fails("tasks", "--refresh")["error"], "auth")


# ---------------------------------------------------------------- reads


class ReadTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.login()
        self.fake.add_task(title="late thing", projectId="p-work",
                           dueDate="2020-01-01T09:00:00.000+0000", priority=5)
        self.fake.add_task(title="inbox thing", projectId=self.fake.inbox_id)
        self.fake.add_task(title="home thing", projectId="p-home",
                           dueDate="2099-01-01T09:00:00.000+0000")

    def test_a_whole_account_view_costs_one_task_request(self):
        self.fake.calls.clear()
        self.ok("tasks", "--view", "all", "--refresh", "--include-undated")
        filters = [c for c in self.fake.calls if c[1].endswith("/task/filter")]
        per_project = [c for c in self.fake.calls if c[1].endswith("/data")]
        self.assertEqual(len(filters), 1)
        self.assertEqual(per_project, [],
                         "fanning out per project is what trips the rate limiter")

    def test_the_inbox_is_synthesised_and_closed_projects_are_dropped(self):
        names = [p["name"] for p in self.ok("projects")["projects"]]
        self.assertIn("Inbox", names)
        self.assertNotIn("Archive", names)

    def test_overdue_lands_in_the_overdue_bucket(self):
        rows = self.ok("tasks", "--view", "all", "--include-undated")["tasks"]
        late = next(r for r in rows if r["title"] == "late thing")
        self.assertEqual(late["bucket"], "overdue")
        self.assertEqual(late["project"], "Work")

    def test_every_row_key_is_present_and_non_null(self):
        required = {"id", "projectId", "project", "projectColor", "title", "content",
                    "bucket", "due", "dueLabel", "dueIso", "priority", "isAllDay",
                    "repeat", "reminders", "items", "itemsDone", "itemsTotal"}
        for row in self.ok("tasks", "--view", "all", "--include-undated")["tasks"]:
            self.assertGreaterEqual(set(row), required, f"missing {required - set(row)}")
            for key in required:
                self.assertIsNotNone(row[key], f"{key} is null on {row['id']}")

    def test_undated_tasks_are_hidden_until_asked_for(self):
        self.assertNotIn("inbox thing", self.titles("tasks", "--view", "all"))
        self.assertIn("inbox thing",
                      self.titles("tasks", "--view", "all", "--include-undated"))

    def test_search_endpoint_reaches_completed_tasks(self):
        self.fake.add_task(title="findme done", projectId="p-work", status=2,
                           completedTime="2026-07-01T10:00:00.000+0000")
        self.fake.add_task(title="findme open", projectId="p-work")
        self.assertEqual(self.ok("search", "findme")["count"], 1)
        self.assertEqual(self.ok("search", "findme", "--all-states")["count"], 2)

    def test_tags_are_listed_with_label_and_colour(self):
        tags = self.ok("tags")["tags"]
        self.assertEqual({t["name"] for t in tags}, {"urgent", "errand"})
        self.assertEqual(next(t for t in tags if t["name"] == "errand")["label"], "Errand")


# ------------------------------------------------------- local-first


class LocalFirstTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.login()
        self.task = self.fake.add_task(title="walk the dog", projectId="p-work")
        self.ok("tasks", "--view", "all", "--include-undated", "--refresh")

    def test_a_read_is_served_from_cache_without_touching_the_network(self):
        self.fake.calls.clear()
        payload = self.ok("tasks", "--view", "all", "--include-undated")
        self.assertEqual(payload["source"], "cache")
        self.assertEqual(self.fake.calls, [], "a fresh cache must not hit the network")

    def test_refresh_forces_a_fetch_however_fresh_the_cache_is(self):
        self.fake.calls.clear()
        self.assertEqual(
            self.ok("tasks", "--view", "all", "--refresh")["source"], "network")
        self.assertTrue(self.fake.calls)

    def test_completing_offline_applies_locally_and_queues(self):
        payload = self.ok("complete", self.task["id"], "--offline")
        self.assertTrue(payload["completed"])
        self.assertEqual(payload["pending"], 1)
        # Gone from the local list immediately...
        self.assertNotIn("walk the dog",
                         self.titles("tasks", "--view", "all", "--include-undated", "--offline"))
        # ...and untouched upstream until a sync happens.
        self.assertEqual(int(self.fake.tasks[self.task["id"]]["status"]), 0)

        synced = self.ok("sync")
        self.assertEqual(synced["pending"], 0)
        self.assertEqual(int(self.fake.tasks[self.task["id"]]["status"]), 2)

    def test_a_task_created_offline_syncs_and_adopts_its_real_id(self):
        created = self.ok("add", "buy milk tomorrow", "--offline")
        local_id = created["task"]["id"]
        self.assertTrue(store.is_local_id(local_id))
        self.assertEqual(created["pending"], 1)

        self.ok("sync")
        titles = [t["title"] for t in self.fake.tasks.values()]
        self.assertIn("buy milk", titles)
        # The locally-minted id must not survive the round trip.
        state = store.load()
        self.assertEqual(store.pending(state), 0)
        self.assertFalse(any(store.is_local_id(t["id"]) for t in state["tasks"]),
                         "a synced task must adopt the id the server issued")

    def test_edits_made_offline_against_a_local_task_still_land(self):
        # The hard case: create offline, then edit that not-yet-real task offline.
        created = self.ok("add", "draft post", "--offline")
        local_id = created["task"]["id"]
        self.ok("edit", local_id, "--priority", "high", "--offline")
        self.assertEqual(store.pending(store.load()), 2)

        self.ok("sync")
        self.assertEqual(store.pending(store.load()), 0)
        match = [t for t in self.fake.tasks.values() if t["title"] == "draft post"]
        self.assertEqual(len(match), 1, "the create must not be replayed twice")
        self.assertEqual(int(match[0]["priority"]), 5,
                         "the queued edit must be remapped onto the real id")

    def test_the_queue_survives_a_new_process(self):
        self.ok("complete", self.task["id"], "--offline")
        # `store.load()` is a fresh read of the file, which is all a new process does.
        self.assertEqual(store.pending(store.load()), 1)

    def test_ordering_is_preserved_when_a_flush_stops_early(self):
        second = self.fake.add_task(title="second", projectId="p-work")
        self.ok("tasks", "--refresh", "--view", "all", "--include-undated")
        self.ok("complete", self.task["id"], "--offline")
        self.ok("complete", second["id"], "--offline")
        self.assertEqual(store.pending(store.load()), 2)

        self.fake.offline = False
        self.fake.fail_next = 99  # nothing can get through
        self.ok("sync")
        self.assertEqual(store.pending(store.load()), 2, "a failed flush keeps its queue")

        self.fake.fail_next = 0
        self.ok("sync")
        self.assertEqual(store.pending(store.load()), 0)
        self.assertEqual(int(self.fake.tasks[self.task["id"]]["status"]), 2)
        self.assertEqual(int(self.fake.tasks[second["id"]]["status"]), 2)

    def test_a_stale_cache_still_answers_when_the_network_is_gone(self):
        self.fake.auth_fails = False
        self.fake.fail_next = 99
        payload = self.ok("tasks", "--view", "all", "--include-undated", "--refresh")
        self.assertIn("walk the dog", [t["title"] for t in payload["tasks"]])
        self.assertTrue(payload["warning"], "a degraded read should say so")

    def test_a_write_the_server_permanently_refuses_is_dropped_not_retried_forever(self):
        # Completing a task that no longer exists upstream 404s every time; keeping it
        # queued would wedge every later write behind it.
        del self.fake.tasks[self.task["id"]]
        self.ok("complete", self.task["id"])
        self.assertEqual(store.pending(store.load()), 0)


# ------------------------------------------------------------ checklists


class ChecklistTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.login()
        self.task = self.fake.add_task(
            title="pack for trip", projectId="p-work",
            items=[{"id": "i1", "title": "passport", "status": 0},
                   {"id": "i2", "title": "charger", "status": 0}],
        )
        self.ok("tasks", "--view", "all", "--include-undated", "--refresh")

    def items(self) -> list[dict]:
        return self.fake.tasks[self.task["id"]]["items"]

    def test_check_and_uncheck_round_trip(self):
        payload = self.ok("check", self.task["id"], "i1")
        self.assertTrue(payload["done"])
        self.assertEqual(int(self.items()[0]["status"]), 1)

        payload = self.ok("check", self.task["id"], "i1", "--uncheck")
        self.assertFalse(payload["done"])
        self.assertEqual(int(self.items()[0]["status"]), 0)
        self.assertIsNone(self.items()[0].get("completedTime"),
                          "a stale completedTime makes TickTick show it done again")

    def test_add_rename_remove_and_reorder(self):
        self.ok("item", "add", self.task["id"], "toothbrush")
        self.assertEqual([i["title"] for i in self.items()],
                         ["passport", "charger", "toothbrush"])

        self.ok("item", "rename", self.task["id"], "i2", "phone charger")
        self.assertEqual(self.items()[1]["title"], "phone charger")

        self.ok("item", "move", self.task["id"], "i2", "0")
        self.assertEqual([i["title"] for i in self.items()][0], "phone charger")

        self.ok("item", "remove", self.task["id"], "i1")
        self.assertNotIn("passport", [i["title"] for i in self.items()])

    def test_an_item_added_offline_gets_a_server_id_on_sync(self):
        self.ok("item", "add", self.task["id"], "sunscreen", "--offline")
        self.assertEqual(store.pending(store.load()), 1)
        self.ok("sync")
        titles = [i["title"] for i in self.items()]
        self.assertIn("sunscreen", titles)
        for item in self.items():
            self.assertFalse(store.is_local_id(item["id"]),
                             "the server assigns item ids; a local one must not persist")

    def test_a_missing_item_is_notfound_not_a_crash(self):
        self.assertEqual(
            self.fails("check", self.task["id"], "nope")["error"], "notfound")

    def test_checklist_progress_is_counted_on_the_row(self):
        self.ok("check", self.task["id"], "i1")
        row = next(t for t in self.ok("tasks", "--view", "all", "--include-undated",
                                      "--refresh")["tasks"]
                   if t["id"] == self.task["id"])
        self.assertEqual((row["itemsDone"], row["itemsTotal"]), (1, 2))


# ------------------------------------------------------------- writes


class WriteTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.login()
        self.ok("tasks", "--view", "all", "--refresh", "--include-undated")

    def test_add_parses_natural_language_and_lands_upstream(self):
        payload = self.ok("add", "submit report tomorrow 5pm !high #work")
        self.assertEqual(payload["task"]["title"], "submit report")
        self.assertEqual(payload["task"]["priority"], 5)
        created = next(t for t in self.fake.tasks.values() if t["title"] == "submit report")
        self.assertEqual(created["projectId"], "p-work")

    def test_every_date_on_the_wire_carries_a_utc_offset(self):
        # A bare date is accepted with 200 and silently discarded; the task would come
        # back undated and nobody would find out.
        self.ok("add", "pay rent 2027-03-01")
        created = next(t for t in self.fake.tasks.values() if t["title"] == "pay rent")
        self.assertIsNotNone(created["dueDate"],
                             "the date was dropped — it must have gone out with an offset")

    def test_an_unknown_project_name_still_files_the_task(self):
        payload = self.ok("add", "something #nosuchproject")
        self.assertEqual(payload["unresolvedProject"], "nosuchproject")
        self.assertIn("something", [t["title"] for t in self.fake.tasks.values()])

    def test_tags_can_be_attached_on_create(self):
        self.ok("add", "call plumber", "--tag", "urgent", "--tag", "#errand")
        created = next(t for t in self.fake.tasks.values() if t["title"] == "call plumber")
        self.assertEqual(set(created["tags"]), {"urgent", "errand"})

    def test_moving_a_task_uses_the_move_endpoint(self):
        task = self.fake.add_task(title="relocate", projectId="p-work")
        self.ok("tasks", "--refresh", "--view", "all", "--include-undated")
        self.fake.calls.clear()
        self.ok("edit", task["id"], "--move-to", "p-home")
        self.ok("sync")
        self.assertEqual(self.fake.tasks[task["id"]]["projectId"], "p-home")
        self.assertTrue(any(c[1].endswith("/task/move") for c in self.fake.calls),
                        "rewriting projectId in an update body does not move a task")

    def test_delete_removes_it_upstream(self):
        task = self.fake.add_task(title="doomed", projectId="p-work")
        self.ok("tasks", "--refresh", "--view", "all", "--include-undated")
        self.ok("delete", task["id"], "--yes")
        self.ok("sync")
        self.assertNotIn(task["id"], self.fake.tasks)

    def test_edit_rewrites_the_whole_task_because_there_is_no_patch(self):
        task = self.fake.add_task(title="keep me", projectId="p-work", content="notes here")
        self.ok("tasks", "--refresh", "--view", "all", "--include-undated")
        self.ok("edit", task["id"], "--priority", "medium")
        self.ok("sync")
        self.assertEqual(int(self.fake.tasks[task["id"]]["priority"]), 3)
        self.assertEqual(self.fake.tasks[task["id"]]["content"], "notes here",
                         "a full-body update must not drop the fields it did not change")

    def test_contradictory_flags_are_a_usage_error(self):
        self.assertEqual(
            self.fails("edit", "x", "--due", "today", "--clear-due")["error"], "usage")
        self.assertEqual(self.fails("edit", "x")["error"], "usage")

    def test_project_delete_demands_confirmation(self):
        self.assertEqual(self.fails("project", "delete", "p-work")["error"], "usage")
        self.assertIn("p-work", [p["id"] for p in self.fake.projects])
        self.ok("project", "delete", "p-work", "--yes")
        self.assertNotIn("p-work", [p["id"] for p in self.fake.projects])


# ------------------------------------------------- failure and abuse


class ResilienceTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.login()
        self.fake.add_task(title="survivor", projectId="p-work")

    def test_throttling_arrives_as_a_500_and_is_retried_then_reported(self):
        # TickTick signals its rate limit with an HTTP 500 whose body says
        # exceed_query_limit. Treating status alone as the signal misses it entirely.
        self.fake.throttle_next = 2
        payload = self.ok("tasks", "--view", "all", "--refresh", "--include-undated")
        self.assertIn("survivor", [t["title"] for t in payload["tasks"]])

    def test_persistent_throttling_backs_off_across_processes(self):
        self.ok("tasks", "--refresh", "--view", "all", "--include-undated")
        self.ok("complete", store.load()["tasks"][0]["id"], "--offline")
        self.fake.throttle_next = 99
        self.ok("sync")

        state = store.load()
        self.assertEqual(store.pending(state), 1)
        self.assertGreater(store.blocked_for(state), 0,
                           "a rate limit must outlive the process that hit it")

        # A second invocation must not hammer straight through the backoff.
        self.fake.calls.clear()
        self.ok("sync")
        self.assertEqual(
            [c for c in self.fake.calls if "complete" in c[1]], [],
            "the queued write should not be retried while the backoff is in force")

    def test_a_garbage_response_is_an_api_error_not_a_traceback(self):
        self.fake.garbage_next = 99
        payload = self.run_cli("tasks", "--view", "all", "--refresh")
        self.assertIn("ok", payload)

    def test_a_dead_network_degrades_to_the_cache(self):
        self.ok("tasks", "--refresh", "--view", "all", "--include-undated")
        self.server.__exit__(None, None, None)  # nothing is listening any more
        payload = self.ok("tasks", "--view", "all", "--include-undated", "--refresh")
        self.assertIn("survivor", [t["title"] for t in payload["tasks"]])
        self.assertTrue(payload["warning"])

    def test_a_corrupt_state_file_is_rebuilt_not_fatal(self):
        store.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        store.STATE_PATH.write_text("{not json at all", encoding="utf-8")
        payload = self.ok("tasks", "--view", "all", "--refresh", "--include-undated")
        self.assertIn("survivor", [t["title"] for t in payload["tasks"]])

    def test_a_corrupt_credentials_file_reads_as_signed_out(self):
        config.CONFIG_PATH.write_text("]]not json[[", encoding="utf-8")
        self.assertEqual(self.fails("tasks", "--refresh")["error"], "auth")

    def test_the_state_file_is_not_world_readable(self):
        self.ok("tasks", "--refresh", "--view", "all", "--include-undated")
        self.assertEqual(store.STATE_PATH.stat().st_mode & 0o077, 0,
                         "task titles are private")

    def test_the_credential_file_is_not_world_readable(self):
        self.assertEqual(config.CONFIG_PATH.stat().st_mode & 0o077, 0)

    def test_unicode_and_very_long_titles_survive_the_round_trip(self):
        title = "买牛奶 🥛 " + "x" * 300
        self.ok("add", title)
        self.assertIn(title.strip(), [t["title"] for t in self.fake.tasks.values()])

    def test_an_id_containing_a_slash_cannot_change_the_endpoint(self):
        # Path segments are quoted with safe='' precisely so this cannot escape.
        payload = self.run_cli("complete", "../../project/p-work", "--project", "p-work")
        self.assertIn("ok", payload)
        self.assertTrue(all("../" not in path for _, path in self.fake.calls))

    def test_a_huge_account_still_answers_one_request(self):
        for n in range(600):
            self.fake.add_task(title=f"task {n}", projectId="p-work")
        self.fake.calls.clear()
        payload = self.ok("tasks", "--view", "all", "--refresh", "--include-undated")
        self.assertEqual(payload["counts"]["total"], 601)
        self.assertEqual(len([c for c in self.fake.calls if c[1].endswith("/task/filter")]), 1)

    def test_the_outbox_cannot_grow_without_bound(self):
        self.ok("tasks", "--refresh", "--view", "all", "--include-undated")
        state = store.load()
        for n in range(store.MAX_OUTBOX + 25):
            store.enqueue(state, {"op": "complete", "taskId": f"t{n}", "projectId": "p-work"})
        self.assertEqual(store.pending(state), store.MAX_OUTBOX)


class OutputContractTest(Base):
    """The pipe must always carry JSON — the widget parses it and nothing else.

    `--text` once shared a dest with the `text` positional that `add`, `parse` and
    `search` all use, which quietly made those three print prose into the widget's
    pipe. Quick-add and the parse preview broke and nothing else did, so the whole
    class of flag/positional collision gets pinned here.
    """

    def out(self, *argv: str) -> str:
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(list(argv)), 0)
        return buf.getvalue()

    def test_commands_with_a_text_positional_still_emit_json_to_a_pipe(self):
        self.login()
        for argv in (("parse", "buy milk tomorrow"),
                     ("add", "buy milk tomorrow"),
                     ("search", "milk"),
                     ("tasks",)):
            with self.subTest(argv=argv):
                json.loads(self.out(*argv))  # raises if a single line of prose leaked

    def test_a_positional_may_be_literally_dash_dash_json(self):
        self.assertEqual(json.loads(self.out("parse", "--", "--json"))["title"], "--json")
        self.assertEqual(json.loads(self.out("parse", "--", "--text"))["title"], "--text")

    def test_the_flags_work_on_either_side_of_the_subcommand(self):
        for argv in (("--json", "parse", "hello"), ("parse", "hello", "--json")):
            with self.subTest(argv=argv):
                self.assertEqual(json.loads(self.out(*argv))["title"], "hello")

    def test_text_forces_prose_and_json_is_the_default_through_a_pipe(self):
        self.assertNotIn("{", self.out("parse", "hello", "--text").splitlines()[0])
        json.loads(self.out("parse", "hello"))

    def test_every_subcommand_accepts_the_output_flags(self):
        self.login()
        for argv in (("tasks",), ("projects",), ("status",), ("sync",), ("tags",),
                     ("item", "add", "x", "step"), ("project", "groups"),
                     ("complete", "x"), ("check", "x", "i")):
            with self.subTest(argv=argv):
                json.loads(self.out(*argv, "--json"))


if __name__ == "__main__":
    unittest.main()
