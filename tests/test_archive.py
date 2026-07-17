import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
SPEC = importlib.util.spec_from_file_location("archive", ROOT / "src" / "archive.py")
archive = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(archive)


class VerbatimArchiveTests(unittest.TestCase):
    def make_db(self):
        temporary = tempfile.TemporaryDirectory()
        path = Path(temporary.name) / "things.sqlite"
        connection = sqlite3.connect(path)
        connection.executescript(
            '''
            CREATE TABLE TMArea(uuid TEXT PRIMARY KEY,title TEXT);
            CREATE TABLE TMTag(uuid TEXT PRIMARY KEY,title TEXT);
            CREATE TABLE TMTaskTag(tasks TEXT,tags TEXT);
            CREATE TABLE TMChecklistItem(
              uuid TEXT PRIMARY KEY,task TEXT,title TEXT,status INTEGER,
              leavesTombstone INTEGER,"index" INTEGER,creationDate REAL,stopDate REAL
            );
            CREATE TABLE TMTask(
              uuid TEXT PRIMARY KEY,type INTEGER,status INTEGER,trashed INTEGER,
              title TEXT,notes TEXT,stopDate REAL,area TEXT,project TEXT,heading TEXT,
              creationDate REAL,userModificationDate REAL,rt1_repeatingTemplate TEXT,"index" INTEGER
            );
            INSERT INTO TMArea VALUES('area','Personal');
            '''
        )
        rows = [
            ("spermogram", 0, 3, 0, "Сделать спермограмму", "", 1000, "area", None, None, 900, 1000, None, 0),
            ("decision", 0, 3, 0, " Выбор ", " \nРешил выбрать вариант A, потому что он проще\n ", 1100, "area", None, None, 900, 1100, None, 1),
            ("history", 0, 3, 0, "История", "Результат 42 кг", 1200, "area", None, None, 900, 1200, None, 2),
            ("routine", 0, 2, 0, "Купить молоко", "", 1300, "area", None, None, 900, 1300, None, 3),
        ]
        connection.executemany("INSERT INTO TMTask VALUES(" + ",".join("?" for _ in rows[0]) + ")", rows)
        connection.execute("INSERT INTO TMChecklistItem VALUES('check','decision',' Exact checklist ',3,0,0,900,1100)")
        connection.execute("INSERT INTO TMTag VALUES('tag','Important')")
        connection.execute("INSERT INTO TMTaskTag VALUES('decision','tag')")
        connection.commit()
        connection.close()
        return temporary, path

    def test_stage_label_and_index_are_private_and_verbatim(self):
        temporary, source = self.make_db()
        try:
            run = Path(temporary.name) / "archive-run"
            database = archive.create_archive(source, run)
            profile = archive.archive_profile(database)
            self.assertEqual(4, profile["events"])
            self.assertEqual(1, profile["dated_event_hints"])
            self.assertEqual(0o700, run.stat().st_mode & 0o777)
            self.assertEqual(0o600, database.stat().st_mode & 0o777)

            connection = archive.connect(database)
            try:
                row = connection.execute("SELECT * FROM event WHERE id='spermogram'").fetchone()
                self.assertEqual("", row["notes"])
                self.assertEqual(1, row["dated_event_hint"])
                decision = connection.execute("SELECT * FROM event WHERE id='decision'").fetchone()
                self.assertEqual(" Выбор ", decision["title"])
                self.assertEqual(" \nРешил выбрать вариант A, потому что он проще\n ", decision["notes"])
                self.assertEqual("[completed]  Exact checklist ", decision["checklist_text"])
                payload = json.loads(decision["payload_json"])
                self.assertEqual(" Exact checklist ", payload["checklist"][0]["title"])
            finally:
                connection.close()

            sample = run / "sample.json"
            review = run / "review.html"
            archive.write_review(database, sample, review, 4)
            source_hash = json.loads(sample.read_text())["source_sha256"]
            labels = run / "labels.json"
            labels.write_text(
                json.dumps(
                    {
                        "schema": archive.LABEL_SCHEMA,
                        "source_sha256": source_hash,
                        "labels": [
                            {"event_id": "spermogram", "categories": ["dated_event_fact"], "disposition": None},
                            {"event_id": "history", "categories": [], "disposition": "drop"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            imported = archive.import_labels(database, labels)
            self.assertEqual(2, imported["labels"])

            index_json = run / "knowledge-index.json"
            index_html = run / "knowledge-index.html"
            report = archive.build_knowledge_index(database, index_json, index_html)
            indexed = json.loads(index_json.read_text())["records"]
            by_id = {value["event_id"]: value for value in indexed}
            self.assertIn("spermogram", by_id)
            self.assertNotIn("history", by_id)
            self.assertEqual("", by_id["spermogram"]["notes"])
            self.assertEqual("things:///show?id=spermogram", by_id["spermogram"]["things_url"])
            self.assertEqual("area", by_id["spermogram"]["area_id"])
            self.assertEqual("Personal", by_id["spermogram"]["area"])
            self.assertIsNone(by_id["spermogram"]["project"])
            self.assertEqual([], by_id["spermogram"]["tags"])
            self.assertEqual([], by_id["spermogram"]["checklist_items"])
            self.assertEqual(1, report["labeled_drops_excluded"])
            self.assertEqual(0o600, index_json.stat().st_mode & 0o777)
            self.assertEqual(0o600, index_html.stat().st_mode & 0o777)
            if node := shutil.which("node"):
                document = index_html.read_text(encoding="utf-8")
                script = document.rsplit("<script>", 1)[1].split("</script>", 1)[0]
                checked = subprocess.run([node, "--check"], input=script, text=True, capture_output=True)
                self.assertEqual(0, checked.returncode, checked.stderr)
        finally:
            temporary.cleanup()

    def test_schema_guard_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bad.sqlite"
            sqlite3.connect(source).close()
            with self.assertRaises(archive.support.ArchiveSourceError):
                archive.create_archive(source, Path(directory) / "out")


if __name__ == "__main__":
    unittest.main()
