"""Tests for ticktick.views' fetch path.

Everything here is undated on purpose: bucketing is `dates`' business and is covered in
test_dates.py, so these fixtures never touch the clock. `project_data_many` is a landmine
in the fake client — the whole point of the rewrite is that no view fans out any more.
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ticktick import views  # noqa: E402

INBOX = "inbox119511654"


def task(tid: str, pid: str, title: str = "t") -> dict:
    return {"id": tid, "projectId": pid, "title": title}


class FakeClient:
    """Records what was called, so a reintroduced fan-out fails loudly."""

    def __init__(self, projects: list[dict], tasks: list[dict]) -> None:
        self._projects, self._tasks = projects, tasks
        self.calls: list[str] = []

    def projects(self) -> list[dict]:
        self.calls.append("projects")
        return self._projects

    def all_undone(self) -> list[dict]:
        self.calls.append("all_undone")
        return list(self._tasks)

    def project_data(self, pid: str) -> dict:
        self.calls.append(f"project_data:{pid}")
        return {
            "project": {"id": pid, "name": "Echoed", "color": "#00ff00"},
            "tasks": [t for t in self._tasks if t["projectId"] == pid],
        }

    def inbox_data(self) -> dict:
        self.calls.append("inbox_data")
        return {"tasks": [t for t in self._tasks if t["projectId"].startswith("inbox")]}

    def project_data_many(self, pids, **kwargs):  # noqa: ANN001, ANN003
        raise AssertionError("the per-project fan-out trips TickTick's rate limiter")


def fixture() -> FakeClient:
    return FakeClient(
        [
            {"id": "p1", "name": "Work", "color": "#ff0000"},
            {"id": "p2", "name": "Home", "color": ""},
            {"id": "p3", "name": "Archive", "color": "", "closed": True},
        ],
        [
            task("t1", "p1"),
            task("t2", "p1"),
            task("t3", "p2"),
            task("t4", INBOX),
            task("t5", "gone"),  # a project /project never listed: closed, shared, new
        ],
    )


class FetchTest(unittest.TestCase):
    def test_whole_account_views_make_one_task_request(self):
        client = fixture()
        for view in ("today", "overdue", "next", "all"):
            client.calls.clear()
            views.collect(client, {}, view=view)
            self.assertEqual(client.calls, ["projects", "all_undone"], view)

    def test_project_view_fetches_only_that_project(self):
        client = fixture()
        result = views.collect(client, {}, view="project", project="p1")
        self.assertEqual(client.calls, ["projects", "project_data:p1"])
        self.assertEqual([t["id"] for t in result["tasks"]], ["t1", "t2"])

    def test_project_view_of_the_inbox_uses_the_inbox_path(self):
        # /project/<inbox id>/data is not a thing; only the literal "inbox" path is.
        client = fixture()
        result = views.collect(client, {}, view="project", project=INBOX)
        self.assertEqual(client.calls, ["projects", "inbox_data"])
        self.assertEqual([t["id"] for t in result["tasks"]], ["t4"])
        self.assertEqual(result["tasks"][0]["project"], views.INBOX_NAME)

    def test_unknown_project_still_renders_with_a_blank_name(self):
        rows = views.collect(fixture(), {}, view="all", include_undated=True)["tasks"]
        orphan = [t for t in rows if t["id"] == "t5"]
        self.assertEqual(len(orphan), 1, "a task must never vanish with its project")
        self.assertEqual(orphan[0]["project"], "")
        self.assertEqual(orphan[0]["projectId"], "gone")

    def test_every_project_gets_a_real_count(self):
        result = views.collect(fixture(), {}, view="all", include_undated=True)
        counts = {p["name"]: p["count"] for p in result["projects"]}
        self.assertEqual(counts, {views.INBOX_NAME: 1, "Work": 2, "Home": 1})
        self.assertEqual(result["counts"]["total"], 5)

    def test_the_inbox_id_is_learned_from_the_tasks_and_cached(self):
        cfg: dict = {}
        result = views.collect(fixture(), cfg, view="all", include_undated=True)
        self.assertEqual(cfg["inbox_id"], INBOX)
        self.assertTrue(result["cache_dirty"])
        self.assertEqual(result["projects"][0]["name"], views.INBOX_NAME)
        self.assertEqual([t["project"] for t in result["tasks"] if t["id"] == "t4"],
                         [views.INBOX_NAME])

    def test_a_cached_inbox_id_is_not_rewritten(self):
        cfg = {"inbox_id": INBOX, "project_cache": {t: p for t, p in (
            ("t1", "p1"), ("t2", "p1"), ("t3", "p2"), ("t4", INBOX), ("t5", "gone"))}}
        result = views.collect(fixture(), cfg, view="all", include_undated=True)
        self.assertFalse(result["cache_dirty"], "nothing new was learned")


if __name__ == "__main__":
    unittest.main()
