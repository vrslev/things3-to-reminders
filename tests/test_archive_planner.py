import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
SPEC = importlib.util.spec_from_file_location("archive_planner", ROOT / "src" / "archive_planner.py")
archive_planner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(archive_planner)


class ArchivePlannerTests(unittest.TestCase):
    def record(self, event_id, *, title="Exact title", status="completed", tier="broad", categories=None, notes="Exact notes", checklist="[completed] Exact item", date="2025-04-16T15:56:24.910005+00:00"):
        return {
            "event_id": event_id,
            "categories": categories or ["registered_fact"],
            "tier": tier,
            "score": 1,
            "signals": {},
            "title": title,
            "notes": notes,
            "checklist": checklist,
            "status": status,
            "date": date,
            "context": ["Area", "Project"],
            "area_id": "area-1",
            "area": "Area",
            "project": "Project",
            "heading": "Heading",
            "tags": ["Important"],
            "checklist_items": ([{"title": "Exact item", "status": "completed"}] if checklist else []),
            "global_title_count": 1,
            "repeat_series_size": 0,
            "things_url": f"things:///show?id={event_id}",
        }

    def document(self):
        return {
            "schema": archive_planner.INDEX_SCHEMA,
            "source_sha256": "a" * 64,
            "created_at": "2026-07-16T00:00:00+00:00",
            "policy": "test",
            "validation": {},
            "records": [
                self.record("confirmed", tier="human_keep", categories=["decision"]),
                self.record("canceled", status="canceled", categories=["insight"]),
                self.record("empty", title="", notes="", categories=["dated_event_fact"]),
                self.record("fact", categories=["registered_fact"]),
            ],
        }

    def test_builds_completed_archive_items_by_area_with_native_dates(self):
        plan, report = archive_planner.build_archive_plan(
            self.document(), "archive-run", "Things Archive", None
        )
        self.assertEqual("things-reminders-plan/v5", plan["schema"])
        self.assertEqual("verbatim_archive", plan["planKind"])
        self.assertEqual(["Things Archive — archive-run — Area"], plan["calendarTitles"])
        self.assertEqual(4, len(plan["items"]))
        item = next(value for value in plan["items"] if value["sourceID"] == "confirmed")
        self.assertTrue(item["completed"])
        self.assertEqual("2025-04-16T15:56:24.910Z", item["completionDate"])
        self.assertEqual("2025-04-16", item["dueDate"])
        self.assertIsNone(item["alarmDate"])
        self.assertEqual([], item["recurrenceRules"])
        self.assertEqual("Exact title", item["title"])
        self.assertEqual(
            "Exact notes\n\nProject: Project\nHeading: Heading\nTags: #Important\nChecklist:\n- [x] Exact item",
            item["notes"],
        )
        self.assertNotIn("Things archive metadata", item["notes"])
        self.assertNotIn("Archive categories", item["notes"])
        self.assertNotIn("Classification:", item["notes"])
        self.assertEqual("things:///show?id=confirmed", item["url"])
        self.assertIn("canceled=1", report)
        self.assertIn("No Apple Reminders data has been changed", report)

    def test_duplicate_area_titles_get_distinct_archive_lists(self):
        document = self.document()
        duplicate = self.record("other-area")
        duplicate["area_id"] = "area-2"
        document["records"].append(duplicate)
        plan, _ = archive_planner.build_archive_plan(
            document, "archive-run", "Things Archive", None
        )
        lists = {item["sourceID"]: item["listTitle"] for item in plan["items"]}
        self.assertNotEqual(lists["confirmed"], lists["other-area"])
        self.assertIn("[area-1]", lists["confirmed"])
        self.assertIn("[area-2]", lists["other-area"])

    def test_missing_area_uses_no_area_list_name(self):
        document = self.document()
        no_area = self.record("no-area")
        no_area["area_id"] = None
        no_area["area"] = None
        no_area["context"] = ["Project"]
        document["records"].append(no_area)
        plan, _ = archive_planner.build_archive_plan(
            document, "archive-run", "Things Archive", None
        )
        item = next(value for value in plan["items"] if value["sourceID"] == "no-area")
        self.assertEqual("Things Archive — archive-run — No Area", item["listTitle"])

    def test_empty_title_uses_visible_label_and_preserves_empty_source(self):
        plan, _ = archive_planner.build_archive_plan(
            self.document(), "archive-run", "Things Archive", None
        )
        item = next(value for value in plan["items"] if value["sourceID"] == "empty")
        self.assertEqual("", item["sourceTitle"])
        self.assertEqual("Untitled Things archive item [empty]", item["title"])
        self.assertEqual(
            "Project: Project\nHeading: Heading\nTags: #Important\nChecklist:\n- [x] Exact item",
            item["notes"],
        )
        self.assertNotIn("Original Things title", item["notes"])
        self.assertTrue(item["warnings"])
        self.assertTrue(any("empty source titles" in value for value in plan["warnings"]))

    def test_pilot_keeps_human_labels_and_eventkit_edges(self):
        document = self.document()
        for position in range(30):
            document["records"].append(self.record(f"extra-{position}"))
        selected, policy = archive_planner.select_records(document, 10)
        ids = {value["event_id"] for value in selected}
        self.assertEqual(10, len(selected))
        self.assertIn("confirmed", ids)
        self.assertIn("empty", ids)
        self.assertIn("canceled", ids)
        self.assertIn("edge cases", policy)

    def test_rejects_duplicate_ids_and_mismatched_things_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.json"
            document = self.document()
            document["records"].append(dict(document["records"][0]))
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(archive_planner.planner.PlanError, "duplicate event_id"):
                archive_planner.read_index(path)

            document = self.document()
            document["records"][0]["things_url"] = "things:///show?id=wrong"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(archive_planner.planner.PlanError, "mismatched Things URL"):
                archive_planner.read_index(path)

    def test_command_writes_private_auditable_run_without_external_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index = root / "index.json"
            run = root / "run"
            index.write_text(json.dumps(self.document()), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                result = archive_planner.main([
                    "--index", str(index), "--run-dir", str(run), "--run-id", "archive-test", "--count", "3",
                ])
            self.assertEqual(0, result)
            plan = json.loads((run / "plan.json").read_text())
            self.assertEqual(3, len(plan["items"]))
            self.assertEqual("planned", json.loads((run / "state.json").read_text())["status"])
            self.assertEqual(index.read_bytes(), (run / "source-index.json").read_bytes())
            self.assertEqual(0o700, run.stat().st_mode & 0o777)
            self.assertEqual(0o600, (run / "plan.json").stat().st_mode & 0o777)
            self.assertIn("No Apple Reminders data has been changed", (run / "report.txt").read_text())


if __name__ == "__main__":
    unittest.main()
