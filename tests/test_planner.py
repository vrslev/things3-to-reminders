import datetime
import importlib.util
import plistlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("planner", Path(__file__).parents[1] / "src" / "planner.py")
planner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules["planner"] = planner
SPEC.loader.exec_module(planner)


def packed(y, m, d):
    return y << 16 | m << 12 | d << 7


def reminder_time(h, m):
    return h << 26 | m << 20


class PlannerTests(unittest.TestCase):
    def make_db(self, recurrence_mode=0):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "source.sqlite"
        con = sqlite3.connect(path)
        con.executescript('''
        CREATE TABLE Meta(key TEXT PRIMARY KEY, value BLOB);
        CREATE TABLE TMArea(uuid TEXT PRIMARY KEY, title TEXT);
        CREATE TABLE TMTag(uuid TEXT PRIMARY KEY, title TEXT, parent TEXT, "index" INTEGER);
        CREATE TABLE TMTaskTag(tasks TEXT, tags TEXT);
        CREATE TABLE TMChecklistItem(uuid TEXT PRIMARY KEY, title TEXT, status INTEGER, task TEXT, "index" INTEGER, leavesTombstone INTEGER);
        CREATE TABLE TMTask(
          uuid TEXT PRIMARY KEY, type INTEGER, status INTEGER, trashed INTEGER,
          title TEXT, notes TEXT, start INTEGER, startDate INTEGER,
          reminderTime INTEGER, deadline INTEGER, area TEXT, project TEXT, heading TEXT,
          rt1_repeatingTemplate TEXT, rt1_recurrenceRule BLOB,
          rt1_instanceCreationStartDate INTEGER, rt1_instanceCreationPaused INTEGER,
          rt1_instanceCreationCount INTEGER, rt1_afterCompletionReferenceDate INTEGER,
          rt1_nextInstanceStartDate INTEGER, creationDate REAL, userModificationDate REAL
        );
        ''')
        con.execute("INSERT INTO Meta VALUES('databaseVersion', ?)", (plistlib.dumps(26),))
        con.execute("INSERT INTO TMArea VALUES('area-1','Work')")
        con.execute("INSERT INTO TMTag VALUES('tag-1','Important',NULL,0)")
        con.execute("INSERT INTO TMTask VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            'normal',0,0,0,'Normal task','Notes',1,packed(2026,7,20),reminder_time(9,30),packed(2026,7,21),'area-1',None,None,
            None,None,None,0,0,None,None,1.0,1.0
        ))
        rule = plistlib.dumps({
            'ed': 64092211200.0, 'fa': 1, 'fu': 256, 'ia': 1784505600.0,
            'of': [{'wd': 1}, {'wd': 3}], 'rc': 0, 'rrv': 4,
            'sr': 1784505600.0, 'tp': recurrence_mode, 'ts': -2,
        })
        con.execute("INSERT INTO TMTask VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            'template',0,0,0,'Recurring task','Recurring notes',2,None,reminder_time(8,0),planner.RECURRING_DEADLINE_PLACEHOLDER,'area-1',None,None,
            None,rule,packed(2026,7,19),0,1,None,packed(2026,7,20),2.0,2.0
        ))
        con.execute("INSERT INTO TMTask VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            'instance',0,0,0,'Recurring task','Recurring notes',1,packed(2026,7,20),None,packed(2026,7,22),'area-1',None,None,
            'template',None,None,0,0,None,None,3.0,3.0
        ))
        con.execute("INSERT INTO TMTaskTag VALUES('normal','tag-1')")
        con.execute("INSERT INTO TMChecklistItem VALUES('c1','Subtask',0,'normal',0,0)")
        con.commit(); con.close()
        return tmp, path

    def add_recurring_project(self, path):
        con = sqlite3.connect(path)
        rule = plistlib.dumps({
            'ed': 64092211200.0, 'fa': 1, 'fu': 256, 'ia': 1784505600.0,
            'of': [{'wd': 0}], 'rc': 0, 'rrv': 4,
            'sr': 1784505600.0, 'tp': 0, 'ts': 0,
        })
        # Recurring project template and its static blueprint.
        con.execute("INSERT INTO TMTask VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            'project-template',1,0,0,'Weekly review','Project notes',2,None,reminder_time(10,0),planner.RECURRING_DEADLINE_PLACEHOLDER,'area-1',None,None,
            None,rule,packed(2026,7,19),0,1,None,packed(2026,7,20),4.0,4.0
        ))
        con.execute("INSERT INTO TMTask VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            'project-heading',2,0,0,'Preparation','',1,None,None,None,None,'project-template',None,
            None,None,None,0,0,None,None,4.1,4.1
        ))
        con.execute("INSERT INTO TMTask VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            'project-child-1',0,0,0,'Collect metrics','Export dashboard first\nMigration-ID: not-a-real-run',1,None,None,None,None,'project-template',None,
            None,None,None,0,0,None,None,4.2,4.2
        ))
        con.execute("INSERT INTO TMTask VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            'project-child-2',0,0,0,'Write summary','',1,None,None,None,None,None,'project-heading',
            None,None,None,0,0,None,None,4.3,4.3
        ))
        # Current generated project occurrence and copied children.
        con.execute("INSERT INTO TMTask VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            'project-instance',1,0,0,'Weekly review','Current project notes',1,packed(2026,7,20),None,packed(2026,7,20),'area-1',None,None,
            'project-template',None,None,0,0,None,None,5.0,5.0
        ))
        con.execute("INSERT INTO TMTask VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            'instance-child-1',0,0,0,'Collect metrics','',1,packed(2026,7,20),None,packed(2026,7,20),None,'project-instance',None,
            None,None,None,0,0,None,None,5.1,5.1
        ))
        con.execute("INSERT INTO TMTaskTag VALUES('project-child-1','tag-1')")
        con.execute("INSERT INTO TMChecklistItem VALUES('pc1','Check source data',0,'project-child-1',0,0)")
        con.commit(); con.close()

    def add_inactive_context_items(self, path):
        con = sqlite3.connect(path)
        # Completed project with an individually-open child: this is Logbook data.
        con.execute("INSERT INTO TMTask VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            'done-project',1,3,0,'Finished project','',1,None,None,None,'area-1',None,None,
            None,None,None,0,0,None,None,10.0,10.0
        ))
        con.execute("INSERT INTO TMTask VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            'stale-child',0,0,0,'Stale child','',1,None,None,None,None,'done-project',None,
            None,None,None,0,0,None,None,10.1,10.1
        ))

        # Active project, but canceled heading with an individually-open child.
        con.execute("INSERT INTO TMTask VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            'active-project',1,0,0,'Active project','',1,None,None,None,'area-1',None,None,
            None,None,None,0,0,None,None,11.0,11.0
        ))
        con.execute("INSERT INTO TMTask VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            'closed-heading',2,2,0,'Old section','',1,None,None,None,None,'active-project',None,
            None,None,None,0,0,None,None,11.1,11.1
        ))
        con.execute("INSERT INTO TMTask VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            'stale-heading-child',0,0,0,'Stale heading child','',1,None,None,None,None,None,'closed-heading',
            None,None,None,0,0,None,None,11.2,11.2
        ))
        con.execute("INSERT INTO TMTask VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            'active-child',0,0,0,'Active child','',1,None,None,None,None,'active-project',None,
            None,None,None,0,0,None,None,11.3,11.3
        ))

        # A recurring to-do inside the completed project must not escape as its own series.
        rule = plistlib.dumps({
            'ed': 64092211200.0, 'fa': 1, 'fu': 16, 'ia': 1784505600.0,
            'of': [{'dy': 0}], 'rc': 0, 'rrv': 4,
            'sr': 1784505600.0, 'tp': 0, 'ts': 0,
        })
        con.execute("INSERT INTO TMTask VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            'stale-recurring',0,0,0,'Stale recurring child','',2,None,None,None,None,'done-project',None,
            None,rule,packed(2026,7,19),0,1,None,packed(2026,7,20),12.0,12.0
        ))
        con.commit(); con.close()

    def test_schedule_recurrence_and_no_duplicate_instance(self):
        tmp, path = self.make_db(0)
        try:
            plan, report, blocked = planner.build_plan(path, 'run1', 'Things Import run1', 'abort', False)
            self.assertFalse(blocked, report)
            self.assertEqual('things-reminders-plan/v5', plan['schema'])
            self.assertEqual('main_migration', plan['planKind'])
            self.assertEqual(2, len(plan['items']))
            self.assertTrue(all(item['completed'] is None for item in plan['items']))
            self.assertTrue(all(item['completionDate'] is None for item in plan['items']))
            recurring = next(x for x in plan['items'] if x['sourceID'] == 'template')
            self.assertEqual('instance', recurring['sourceInstanceID'])
            self.assertEqual('2026-07-22', recurring['dueDate'])
            self.assertEqual([4, 6], recurring['recurrenceRules'][0]['weekdays'])
            self.assertNotIn('Repeat:', recurring['notes'])
            self.assertNotIn('Area:', recurring['notes'])
            self.assertNotIn('Things-ID:', recurring['notes'])
            self.assertNotIn('Migration-ID:', recurring['notes'])
            self.assertNotIn('instance', {x['sourceID'] for x in plan['items']})
        finally:
            tmp.cleanup()

    def test_plan_writes_custom_month_day_for_implicit_monthly_rule(self):
        tmp, path = self.make_db(0)
        try:
            monthly = plistlib.dumps({
                'ed': 64092211200.0, 'fa': 1, 'fu': 8, 'ia': 1784592000.0,
                'of': [], 'rc': 0, 'rrv': 4,
                'sr': 1784592000.0, 'tp': 0, 'ts': 0,
            })
            con = sqlite3.connect(path)
            con.execute(
                "UPDATE TMTask SET rt1_recurrenceRule=?, rt1_nextInstanceStartDate=? WHERE uuid='template'",
                (monthly, packed(2026, 7, 21)),
            )
            con.execute(
                "UPDATE TMTask SET startDate=?, deadline=? WHERE uuid='instance'",
                (packed(2026, 7, 21), packed(2026, 7, 21)),
            )
            con.commit(); con.close()

            plan, report, blocked = planner.build_plan(
                path, 'monthly21', 'Things Import monthly21', 'abort', False
            )
            self.assertFalse(blocked, report)
            recurring = next(x for x in plan['items'] if x['sourceID'] == 'template')
            self.assertEqual('monthly', recurring['recurrenceRules'][0]['frequency'])
            self.assertEqual([21], recurring['recurrenceRules'][0]['daysOfMonth'])
            self.assertEqual([], recurring['recurrenceRules'][0]['setPositions'])
        finally:
            tmp.cleanup()

    def test_plan_monthly_first_day_uses_only_day_one(self):
        tmp, path = self.make_db(0)
        try:
            monthly = plistlib.dumps({
                'ed': 64092211200.0, 'fa': 1, 'fu': 8, 'ia': 1782864000.0,
                'of': [{'dy': 0}], 'rc': 0, 'rrv': 4,
                'sr': 1782864000.0, 'tp': 0, 'ts': 0,
            })
            con = sqlite3.connect(path)
            con.execute(
                "UPDATE TMTask SET rt1_recurrenceRule=?, rt1_nextInstanceStartDate=? WHERE uuid='template'",
                (monthly, packed(2026, 8, 1)),
            )
            con.execute(
                "UPDATE TMTask SET startDate=?, deadline=? WHERE uuid='instance'",
                (packed(2026, 8, 1), packed(2026, 8, 1)),
            )
            con.commit(); con.close()

            plan, report, blocked = planner.build_plan(
                path, 'monthly1', 'Things Import monthly1', 'abort', False
            )
            self.assertFalse(blocked, report)
            recurring = next(x for x in plan['items'] if x['sourceID'] == 'template')
            rule = recurring['recurrenceRules'][0]
            self.assertEqual([1], rule['daysOfMonth'])
            self.assertEqual([], rule['setPositions'])
        finally:
            tmp.cleanup()

    def test_children_of_inactive_contexts_are_excluded(self):
        tmp, path = self.make_db(0)
        try:
            self.add_inactive_context_items(path)
            plan, report, blocked = planner.build_plan(path, 'inactive', 'Things Import inactive', 'abort', False)
            self.assertFalse(blocked, report)
            imported = {item['sourceID'] for item in plan['items']}
            self.assertIn('active-child', imported)
            self.assertNotIn('stale-child', imported)
            self.assertNotIn('stale-heading-child', imported)
            self.assertNotIn('stale-recurring', imported)
            excluded = {item['sourceID']: item['reason'] for item in plan['excludedInactiveContext']}
            self.assertIn("Parent project 'Finished project' is completed", excluded['stale-child'])
            self.assertIn("Parent heading 'Old section' is canceled", excluded['stale-heading-child'])
            self.assertIn("Parent project 'Finished project' is completed", excluded['stale-recurring'])
            self.assertIn('Inactive-context items excluded: 3', report)
        finally:
            tmp.cleanup()

    def test_recurring_todo_inside_recurring_project_is_not_imported_separately(self):
        tmp, path = self.make_db(0)
        try:
            self.add_recurring_project(path)
            con = sqlite3.connect(path)
            rule = plistlib.dumps({
                'ed': 64092211200.0, 'fa': 1, 'fu': 16, 'ia': 1784505600.0,
                'of': [{'dy': 0}], 'rc': 0, 'rrv': 4,
                'sr': 1784505600.0, 'tp': 0, 'ts': 0,
            })
            con.execute("INSERT INTO TMTask VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                'nested-repeat',0,0,0,'Nested repeat','',2,None,None,None,None,'project-template',None,
                None,rule,packed(2026,7,19),0,1,None,packed(2026,7,20),6.0,6.0
            ))
            con.commit(); con.close()
            plan, report, blocked = planner.build_plan(
                path, 'nested', 'Things Import nested', 'abort', False,
                recurring_project_policy='summary',
            )
            self.assertFalse(blocked, report)
            imported = {item['sourceID'] for item in plan['items']}
            self.assertIn('project-template', imported)
            self.assertNotIn('nested-repeat', imported)
        finally:
            tmp.cleanup()

    def test_after_completion_aborts_item(self):
        tmp, path = self.make_db(1)
        try:
            plan, _, blocked = planner.build_plan(path, 'run2', 'Things Import run2', 'abort', False)
            self.assertTrue(blocked)
            self.assertEqual(1, len(plan['unsupported']))
        finally:
            tmp.cleanup()

    def test_recurring_project_summary_preserves_blueprint(self):
        tmp, path = self.make_db(0)
        try:
            self.add_recurring_project(path)
            plan, report, blocked = planner.build_plan(
                path, 'run-project', 'Things Import run-project', 'abort', False,
                recurring_project_policy='summary',
            )
            self.assertFalse(blocked, report)
            project = next(x for x in plan['items'] if x['sourceID'] == 'project-template')
            self.assertEqual('project-instance', project['sourceInstanceID'])
            self.assertEqual('2026-07-20', project['dueDate'])
            self.assertTrue(project['recurrenceRules'])
            self.assertIn('Project blueprint', project['notes'])
            self.assertIn('Collect metrics', project['notes'])
            self.assertIn('Export dashboard first', project['notes'])
            self.assertIn('Migration-ID: not-a-real-run', project['notes'])
            self.assertIn('#Important', project['notes'])
            self.assertIn('- [ ] Check source data', project['notes'])
            self.assertIn('Preparation', project['notes'])
            self.assertIn('Write summary', project['notes'])
            imported_sources = {x['sourceID'] for x in plan['items']}
            self.assertNotIn('project-child-1', imported_sources)
            self.assertNotIn('instance-child-1', imported_sources)
            self.assertEqual('summary', plan['recurringProjectPolicy'])
        finally:
            tmp.cleanup()

    def test_recurring_project_manual_policy_excludes_project(self):
        tmp, path = self.make_db(0)
        try:
            self.add_recurring_project(path)
            plan, _, blocked = planner.build_plan(
                path, 'run-project-manual', 'Things Import run-project-manual', 'abort', False,
                unsupported_policy='manual', recurring_project_policy='manual',
            )
            self.assertFalse(blocked)
            self.assertNotIn('project-template', {x['sourceID'] for x in plan['items']})
            self.assertIn('project-template', {x['sourceID'] for x in plan['unsupported']})
        finally:
            tmp.cleanup()


    def test_after_completion_can_be_left_manual_without_blocking_other_items(self):
        tmp, path = self.make_db(1)
        try:
            plan, _, blocked = planner.build_plan(path, 'run4', 'Things Import run4', 'abort', False, 'manual')
            self.assertFalse(blocked)
            self.assertEqual(1, len(plan['items']))
            self.assertEqual('normal', plan['items'][0]['sourceID'])
            self.assertEqual(1, len(plan['unsupported']))
            self.assertEqual('template', plan['unsupported'][0]['sourceID'])
        finally:
            tmp.cleanup()

    def test_monthly_and_yearly_recurrence_decoding(self):
        monthly = plistlib.dumps({
            'ed': 64092211200.0, 'fa': 2, 'fu': 8, 'ia': 1784505600.0,
            'of': [{'dy': 0}, {'dy': 14}], 'rc': 0, 'rrv': 4,
            'sr': 1784505600.0, 'tp': 0, 'ts': 0,
        })
        parsed = planner.parse_recurrence(monthly, 'abort')
        self.assertEqual(2, len(parsed.rules))
        self.assertEqual([1], parsed.rules[0]['daysOfMonth'])
        self.assertEqual([], parsed.rules[0]['setPositions'])
        self.assertEqual([15], parsed.rules[1]['daysOfMonth'])
        self.assertEqual([], parsed.rules[1]['setPositions'])
        self.assertEqual(2, parsed.rules[0]['interval'])
        self.assertEqual(2, parsed.rules[1]['interval'])

        yearly = plistlib.dumps({
            'ed': 64092211200.0, 'fa': 1, 'fu': 4, 'ia': 1784505600.0,
            'of': [{'mo': 0, 'dy': 0}, {'mo': 6, 'dy': 15}], 'rc': 0, 'rrv': 4,
            'sr': 1784505600.0, 'tp': 0, 'ts': 0,
        })
        parsed = planner.parse_recurrence(yearly, 'abort')
        self.assertEqual(2, len(parsed.rules))
        self.assertEqual([1], parsed.rules[0]['monthsOfYear'])
        self.assertEqual([1], parsed.rules[0]['daysOfMonth'])
        self.assertEqual([7], parsed.rules[1]['monthsOfYear'])
        self.assertEqual([16], parsed.rules[1]['daysOfMonth'])

    def test_implicit_monthly_anchor_becomes_explicit_month_day(self):
        monthly = plistlib.dumps({
            'ed': 64092211200.0, 'fa': 1, 'fu': 8, 'ia': 1784592000.0,
            'of': [], 'rc': 0, 'rrv': 4,
            'sr': 1784592000.0, 'tp': 0, 'ts': 0,
        })
        parsed = planner.parse_recurrence(monthly, 'abort')
        self.assertEqual([], parsed.rules[0]['daysOfMonth'])

        explicit = planner.make_monthly_schedule_explicit(
            parsed, datetime.date(2026, 7, 21)
        )
        self.assertIsNotNone(explicit)
        self.assertEqual([21], explicit.rules[0]['daysOfMonth'])
        self.assertEqual([], explicit.rules[0]['setPositions'])

    def test_explicit_monthly_day_is_preserved(self):
        monthly = plistlib.dumps({
            'ed': 64092211200.0, 'fa': 1, 'fu': 8, 'ia': 1784592000.0,
            'of': [{'dy': 20}], 'rc': 0, 'rrv': 4,
            'sr': 1784592000.0, 'tp': 0, 'ts': 0,
        })
        parsed = planner.parse_recurrence(monthly, 'abort')
        explicit = planner.make_monthly_schedule_explicit(
            parsed, datetime.date(2026, 7, 22)
        )
        self.assertEqual([21], explicit.rules[0]['daysOfMonth'])
        self.assertEqual([], explicit.rules[0]['setPositions'])

    def test_exact_month_day_uses_single_explicit_value(self):
        base = {
            'frequency': 'monthly', 'interval': 1, 'weekdays': [],
            'daysOfMonth': [], 'monthsOfYear': [], 'setPositions': [],
            'endDate': None, 'occurrenceCount': None,
        }
        rule = planner.exact_month_day_rule(base, 21)
        self.assertEqual([21], rule['daysOfMonth'])
        self.assertEqual([], rule['setPositions'])

    def test_monthly_last_day_fails_closed(self):
        monthly = plistlib.dumps({
            'ed': 64092211200.0, 'fa': 1, 'fu': 8, 'ia': 1784505600.0,
            'of': [{'dy': -1}], 'rc': 0, 'rrv': 4,
            'sr': 1784505600.0, 'tp': 0, 'ts': 0,
        })
        with self.assertRaisesRegex(planner.PlanError, 'last day of each month'):
            planner.parse_recurrence(monthly, 'abort')

    def test_monthly_fixed_day_and_last_day_fails_closed_as_one_item(self):
        monthly = plistlib.dumps({
            'ed': 64092211200.0, 'fa': 1, 'fu': 8, 'ia': 1784505600.0,
            'of': [{'dy': 14}, {'dy': -1}], 'rc': 0, 'rrv': 4,
            'sr': 1784505600.0, 'tp': 0, 'ts': 0,
        })
        with self.assertRaisesRegex(planner.PlanError, 'last day of each month'):
            planner.parse_recurrence(monthly, 'abort')

    def test_unverified_negative_month_day_fails_closed(self):
        monthly = plistlib.dumps({
            'ed': 64092211200.0, 'fa': 1, 'fu': 8, 'ia': 1784505600.0,
            'of': [{'dy': -2}], 'rc': 0, 'rrv': 4,
            'sr': 1784505600.0, 'tp': 0, 'ts': 0,
        })
        with self.assertRaisesRegex(planner.PlanError, 'no reliable Apple Reminders mapping'):
            planner.parse_recurrence(monthly, 'abort')

    def test_unknown_recurrence_field_fails_closed(self):
        blob = plistlib.dumps({
            'fa': 1, 'fu': 16, 'of': [{'dy': 0}], 'rrv': 4, 'tp': 0,
            'mystery': 123,
        })
        with self.assertRaises(planner.PlanError):
            planner.parse_recurrence(blob, 'abort')

    def test_snapshot_is_independent_of_live_database(self):
        tmp, path = self.make_db(0)
        try:
            snapshot = Path(tmp.name) / 'snapshot.sqlite'
            planner.snapshot_database(path, snapshot)
            live = sqlite3.connect(path)
            live.execute("UPDATE TMTask SET title='Changed later' WHERE uuid='normal'")
            live.commit(); live.close()
            snap = sqlite3.connect(snapshot)
            title = snap.execute("SELECT title FROM TMTask WHERE uuid='normal'").fetchone()[0]
            snap.close()
            self.assertEqual('Normal task', title)
        finally:
            tmp.cleanup()

    def test_after_completion_fixed_is_explicit_warning(self):
        tmp, path = self.make_db(1)
        try:
            plan, _, blocked = planner.build_plan(path, 'run3', 'Things Import run3', 'fixed', False)
            self.assertFalse(blocked)
            item = next(x for x in plan['items'] if x['sourceID'] == 'template')
            self.assertEqual('after_completion', item['recurrenceOriginalMode'])
            self.assertTrue(item['warnings'])
        finally:
            tmp.cleanup()

    def test_template_relative_deadline_without_instance_is_manual(self):
        tmp, path = self.make_db(0)
        try:
            con = sqlite3.connect(path)
            con.execute("DELETE FROM TMTask WHERE uuid='instance'")
            con.commit(); con.close()
            plan, _, blocked = planner.build_plan(path, 'run5', 'Things Import run5', 'abort', False, 'manual')
            self.assertFalse(blocked)
            self.assertEqual(1, len(plan['items']))
            self.assertEqual('normal', plan['items'][0]['sourceID'])
            self.assertEqual(1, len(plan['unsupported']))
            self.assertIn('no open generated occurrence', plan['unsupported'][0]['reason'])
        finally:
            tmp.cleanup()

    def test_generated_instance_without_deadline_uses_start_not_raw_ts(self):
        tmp, path = self.make_db(0)
        try:
            con = sqlite3.connect(path)
            con.execute("UPDATE TMTask SET deadline=NULL WHERE uuid='instance'")
            con.commit(); con.close()
            plan, report, blocked = planner.build_plan(path, 'run6', 'Things Import run6', 'abort', False)
            self.assertFalse(blocked, report)
            recurring = next(x for x in plan['items'] if x['sourceID'] == 'template')
            self.assertEqual('2026-07-20', recurring['dueDate'])
            self.assertEqual([2, 4], recurring['recurrenceRules'][0]['weekdays'])
        finally:
            tmp.cleanup()

    def test_nonzero_internal_recurrence_count_fails_closed(self):
        blob = plistlib.dumps({
            'fa': 1, 'fu': 16, 'of': [{'dy': 0}], 'rc': 4, 'rrv': 4, 'tp': 0,
        })
        with self.assertRaisesRegex(planner.PlanError, 'non-zero recurrence count'):
            planner.parse_recurrence(blob, 'abort')

    def test_paused_recurring_template_is_manual(self):
        tmp, path = self.make_db(0)
        try:
            con = sqlite3.connect(path)
            con.execute("UPDATE TMTask SET rt1_instanceCreationPaused=1 WHERE uuid='template'")
            con.commit(); con.close()
            plan, _, blocked = planner.build_plan(path, 'run7', 'Things Import run7', 'abort', False, 'manual')
            self.assertFalse(blocked)
            self.assertEqual(1, len(plan['unsupported']))
            self.assertIn('paused', plan['unsupported'][0]['reason'].lower())
        finally:
            tmp.cleanup()

    def test_monthly_deadline_offset_is_manual(self):
        tmp, path = self.make_db(0)
        try:
            monthly = plistlib.dumps({
                'ed': 64092211200.0, 'fa': 1, 'fu': 8, 'ia': 1784505600.0,
                'of': [{'dy': 19}], 'rc': 0, 'rrv': 4,
                'sr': 1784505600.0, 'tp': 0, 'ts': 0,
            })
            con = sqlite3.connect(path)
            con.execute("UPDATE TMTask SET rt1_recurrenceRule=? WHERE uuid='template'", (monthly,))
            con.commit(); con.close()
            plan, _, blocked = planner.build_plan(path, 'run8', 'Things Import run8', 'abort', False, 'manual')
            self.assertFalse(blocked)
            self.assertEqual(1, len(plan['unsupported']))
            self.assertIn('monthly/yearly', plan['unsupported'][0]['reason'])
        finally:
            tmp.cleanup()

    def test_notes_are_concise_and_checklists_use_markdown(self):
        notes, url = planner.combine_notes(
            title_context={'area': 'Work', 'project': 'Budget', 'heading': 'Monthly'},
            original_notes='Keep this',
            tags=['Finance'],
            checklist=[{'title': 'Done', 'completed': True}, {'title': 'Next', 'completed': False}],
        )
        self.assertIsNone(url)
        self.assertIn('Keep this', notes)
        self.assertNotIn('Area:', notes)
        self.assertIn('Project: Budget', notes)
        self.assertIn('Heading: Monthly', notes)
        self.assertIn('- [x] Done', notes)
        self.assertIn('- [ ] Next', notes)
        self.assertNotIn('Things-ID:', notes)
        self.assertNotIn('Things-URL:', notes)
        self.assertNotIn('Migration-ID:', notes)
        self.assertNotIn('Repeat:', notes)

    def test_note_only_url_moves_to_url_field(self):
        notes, url = planner.combine_notes(
            title_context={'project': None, 'heading': None},
            original_notes='  https://example.com/path?q=1  ',
            tags=[],
            checklist=[],
        )
        self.assertEqual('', notes)
        self.assertEqual('https://example.com/path?q=1', url)

    def test_url_inside_normal_note_stays_in_notes(self):
        notes, url = planner.combine_notes(
            title_context={'project': None, 'heading': None},
            original_notes='Read https://example.com later',
            tags=[],
            checklist=[],
        )
        self.assertEqual('Read https://example.com later', notes)
        self.assertIsNone(url)

    def test_plan_extracts_note_only_url_and_omits_context_markers(self):
        tmp, path = self.make_db(0)
        try:
            con = sqlite3.connect(path)
            con.execute("UPDATE TMTask SET notes='https://example.com/item' WHERE uuid='normal'")
            con.commit(); con.close()
            plan, report, blocked = planner.build_plan(path, 'run-url', 'Things Import run-url', 'abort', False)
            self.assertFalse(blocked, report)
            item = next(x for x in plan['items'] if x['sourceID'] == 'normal')
            self.assertEqual('https://example.com/item', item['url'])
            self.assertNotIn('https://example.com/item', item['notes'])
            self.assertNotIn('Area:', item['notes'])
            self.assertNotIn('Things-ID:', item['notes'])
            self.assertNotIn('Things-URL:', item['notes'])
            self.assertNotIn('Migration-ID:', item['notes'])
            self.assertNotIn('marker', item)
            self.assertIn('- [ ] Subtask', item['notes'])
        finally:
            tmp.cleanup()

    def test_duplicate_area_titles_get_distinct_lists(self):
        tmp, path = self.make_db(0)
        try:
            con = sqlite3.connect(path)
            con.execute("INSERT INTO TMArea VALUES('area-2','Work')")
            con.execute("INSERT INTO TMTask VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                'normal-2',0,0,0,'Other area task','',1,None,None,None,'area-2',None,None,
                None,None,None,0,0,None,None,4.0,4.0
            ))
            con.commit(); con.close()
            plan, report, blocked = planner.build_plan(path, 'run9', 'Things Import run9', 'abort', False)
            self.assertFalse(blocked, report)
            lists = {x['sourceID']: x['listTitle'] for x in plan['items']}
            self.assertNotEqual(lists['normal'], lists['normal-2'])
            self.assertIn('[area-1]', lists['normal'])
            self.assertIn('[area-2]', lists['normal-2'])
        finally:
            tmp.cleanup()


if __name__ == '__main__':
    unittest.main()
