# Things 3 → Apple Reminders — safe, reversible migrato (100% ChatGPT authored)

A local macOS migration tool with native recurring-reminder support.

It reads a consistent **read-only snapshot** of Things' SQLite database and writes through Apple's public **EventKit** framework. It does not modify Things and does not access the network. Things does not provide an official full-export API, so the reader is necessarily reverse-engineered; schema/version guards make it fail closed rather than continue after an unknown Things change.

A pure Shortcuts workflow cannot create native recurrence rules with Apple's standard “Add New Reminder” action, so the included macOS Shortcut is a GUI launcher for this audited local tool rather than the migration engine itself. See `SHORTCUT.md`.

## What it transfers

For ordinary to-dos that are active **and whose parent heading/project is also active**:

- title and original notes;
- Project, Heading, tags, and checklist as concise Markdown notes;
- a note containing only one HTTP(S) URL is moved to the native Reminders URL field;
- deadline as the Reminders due date, falling back to the Things start date;
- alert time;
- isolated Reminders lists, one per Things Area.

Things may retain child rows with `status=0` after their project or heading has been completed, canceled, or moved to Trash. The planner now checks the entire parent chain and excludes those Logbook/archived rows. Every such exclusion is listed in `report.txt`, `plan.json`, and `MANUAL_REVIEW.md` for audit.

For recurring to-dos, it decodes the Things recurrence plist and creates native EventKit recurrence rules for supported patterns:

- daily;
- weekly, including multiple weekdays;
- monthly by one or more exact days of month;
- yearly by exact month/day;
- interval and end date.

A generated Things occurrence is used for the current title, notes, date, deadline, and checklist, but is **not imported separately**, so the recurring task is not duplicated.

For recurring projects, the default `--recurring-projects=summary` mode creates **one native recurring reminder per project**. Its notes contain a static, always-unchecked blueprint of the project's headings and child to-dos, including child notes, tags, and checklist text. This is the safest reversible representation available in Apple Reminders: EventKit can repeat a reminder, but it cannot repeat a project/list subtree.

Use `--recurring-projects=manual` to exclude recurring projects and put them into `MANUAL_REVIEW.md` instead.

## What is never silently approximated

By default planning stops when it encounters something without a faithful mapping:

- **After Completion** repetition;
- advanced monthly patterns such as “second Tuesday”;
- “last day of every month”, because Apple Reminders does not reliably preserve the required EventKit recurrence;
- monthly/yearly series where the recurring deadline is offset from the recurring start date;
- a relative recurring deadline when Things has no open generated occurrence from which to verify the relationship;
- paused recurring templates;
- non-zero undocumented recurrence-count fields;
- unknown recurrence fields;
- recurring projects only when `--recurring-projects=manual` is selected;
- paused recurring project templates;
- recurring projects whose first occurrence or relative deadline cannot be established safely.

Use `--unsupported=manual` to import everything supported while excluding these items. The run directory then contains `MANUAL_REVIEW.md` with a checkbox, Things link, reason, destination list, dates, and content to copy for every excluded item.

`--after-completion=fixed` is also available, but only use it when you explicitly accept changing “N days after completion” into a fixed calendar schedule. The plan records a warning.

For daily and weekly scheduled repeats, an observed Things start→deadline offset is preserved by shifting the native Reminders recurrence and using a relative alarm. The migrator never guesses that offset from Things' undocumented raw `ts` field.

## v0.3.3 inactive-context fix

Version 0.3.2 filtered only the to-do row itself. In Things, a child can remain individually `open` while its parent project or heading is completed/canceled/trashed and therefore visible only in Logbook. Version 0.3.3 validates the full `to-do → heading → project` chain, excludes inactive-context rows, and prevents recurring child tasks inside a recurring project from being imported as independent series.

## v0.3.7 monthly recurrence correction

Apple Reminders does not reliably honor EventKit `setPositions` for reminders. The previous candidate-range workaround therefore expanded a rule such as “the 1st” into every date from the 1st through the 31st.

Version 0.3.7 uses the direct representation for fixed month days:

- the 1st → `daysOfMonth = [1]`;
- the 21st → `daysOfMonth = [21]`;
- the 15th and 21st → two exact recurrence rules, one for each day.

No `setPositions` value is emitted for these rules. Preflight and apply also reject older plans containing `setPositions`, so an unsafe v0.3.5/v0.3.6 plan cannot be applied accidentally with the new bridge.

Things uses `dy = -1` for “last day of the month”. On the tested Reminders build, both the documented negative month-day and the `BYSETPOS` workaround are normalized incorrectly. Version 0.3.7 therefore excludes last-day rules into `MANUAL_REVIEW.md` instead of inventing an inaccurate schedule.

## Requirements

- macOS;
- Python 3;
- Xcode Command Line Tools: `xcode-select --install`;
- Terminal Full Disk Access may be needed to read the Things group container.

## Recommended workflow

### 1. Check the environment and source

```sh
./things-reminders doctor
./things-reminders inspect
```

### 2. Create a snapshot and reviewable plan

Recommended practical mode: transfer supported items and leave incompatible ones for manual review.

```sh
./things-reminders plan --unsupported=manual
```

The command above imports recurring projects as summary reminders. To leave them for hand transfer:

```sh
./things-reminders plan --unsupported=manual --recurring-projects=manual
```

Strict mode, which refuses to proceed if anything is unsupported:

```sh
./things-reminders plan
```

Intentional conversion of After Completion repeats to fixed schedules:

```sh
./things-reminders plan --unsupported=manual --after-completion=fixed
```

Planning creates a run directory under:

```text
~/Documents/Things Reminders Migration/<run-id>/
```

Nothing has been written to Reminders yet. Review:

- `report.txt` — counts, warnings, unsupported items, and inactive-context exclusions;
- `plan.csv` — compact table;
- `plan.json` — exact machine-readable write plan;
- `MANUAL_REVIEW.md` — unsupported hand-transfer items plus an audit list of inactive-context rows intentionally skipped;
- `source.sqlite` — consistent Things snapshot;
- `MANUAL_ROLLBACK.txt` — manual reversal procedure;
- after apply, `ROLLBACK_INVENTORY.txt` — exact list/reminder EventKit IDs and open URLs, refreshed after every manifest update.

### 3. Preflight EventKit

```sh
./things-reminders preflight "/path/to/run-directory"
```

This compiles and ad-hoc signs the EventKit bridge, requests Reminders access, validates every date and recurrence rule, and confirms a writable Reminders account exists.

### 4. Apply

```sh
./things-reminders apply "/path/to/run-directory"
```

You must type the exact run ID. Apply automatically runs preflight and verification.

### 5. Finish manual review

Open `MANUAL_REVIEW.md`, transfer every unchecked excluded item, and spot-check several recurring reminders before removing anything from Things.

## Safety and resumability

- The live Things database is opened read-only.
- A SQLite backup API snapshot is made before queries run.
- Required tables and columns are validated.
- Database versions 24–26 are accepted only with the expected schema; other versions fail closed unless explicitly overridden.
- Reminders lists have a unique run-ID prefix.
- Parent heading/project state is validated so individually-open Logbook children are not imported.
- `plan.json` is SHA-256 locked as soon as apply begins; a changed plan cannot be resumed.
- The destination Reminders account is locked in the manifest, so a resumed run cannot switch accounts.
- Every created EventKit list/reminder ID is written atomically to `manifest.json` immediately, with a human-readable `ROLLBACK_INVENTORY.txt` refreshed alongside it.
- No migration metadata is inserted into visible reminder notes.
- After a crash, `apply` resumes using recorded EventKit IDs; a save-before-manifest interruption is recovered only by an exact match of all planned reminder fields inside the isolated run list.
- Destination-list collisions, duplicate exact matches, renamed lists, changed partial-run reminders, and account changes abort safely.
- Apply reads each saved reminder back and verifies title, notes, list, URL, date, priority, alarm, recurrence, and final counts.
- `verify` repeats those checks and reports reminders moved after import.

## Reversal

### Scripted rollback

Preview exactly what rollback can identify, without changing anything:

```sh
./things-reminders rollback-preview "/path/to/run-directory"
```

Then roll back:

```sh
./things-reminders rollback "/path/to/run-directory"
```

You must type `ROLLBACK <run-id>`.

Rollback:

- deletes only the exact EventKit reminder IDs recorded in `manifest.json`;
- reports IDs that are already missing instead of searching by title or note content;
- deletes migration-created lists only when empty;
- preserves any list where you later added unrelated reminders;
- records the rollback result in the manifest.

Because v0.3.2+ deliberately keeps notes clean, the run directory is the authoritative rollback identity. Do not discard it before accepting the migration.

### Fully manual rollback

Open `MANUAL_ROLLBACK.txt` in the run directory. In brief:

1. Open `ROLLBACK_INVENTORY.txt` and use the recorded reminder/list open URLs and EventKit IDs.
2. Delete only those exact imported reminders, then delete migration-created lists after confirming they contain nothing else.
3. Do not remove the run directory until the migration is fully accepted.

## Commands

```text
./things-reminders doctor
./things-reminders inspect [--db PATH]
./things-reminders plan [--unsupported=abort|manual] [--after-completion=abort|fixed] [--recurring-projects=summary|manual]
./things-reminders preflight RUN_DIR
./things-reminders apply RUN_DIR
./things-reminders verify RUN_DIR
./things-reminders rollback-preview RUN_DIR
./things-reminders rollback RUN_DIR
```

## Tests

```sh
./run-tests.sh
```

The 29 planner tests cover recurring-instance deduplication, recurring-project summary/manual modes, nested recurring-task suppression, inactive parent project/heading filtering, weekly/monthly/yearly decoding, exact first/21st monthly-day mapping, fail-closed last-day handling, recurring deadline offsets, missing instances, paused templates, undocumented recurrence fields, duplicate Area names, URL extraction, concise Markdown notes, strict After Completion handling, explicit fixed conversion, manual exclusion, snapshot isolation, and schema guards. Swift syntax is also parsed during the test run. EventKit writes themselves must be exercised on macOS because this environment cannot access your Reminders store.

## Why recurring projects are represented as summaries

There are four possible mappings, but only one is sufficiently deterministic for the default migrator:

1. **One recurring reminder with a project blueprint — implemented and recommended.** The repeat schedule is native, all child titles/headings remain visible, and rollback stays exact. Child boxes in the note are descriptive rather than independently completable.
2. **One recurring reminder per child task — not enabled.** This looks closer to a project, but it changes semantics: each child becomes an independent series, completed children in the current Things occurrence are hard to distinguish from future-template children, and monthly/yearly relative offsets cannot always be represented exactly.
3. **A permanent materializer service — not included.** A launch agent could create a fresh batch of one-time reminders for each project occurrence. This is more faithful but turns a one-time migration into an ongoing synchronization service with substantially more failure and rollback states.
4. **Private Reminders APIs or AppleScript subtasks — rejected for the safe path.** They are not a stable public EventKit contract, and recurring-parent/subtask reset behavior is not reliable enough for a fail-closed migrator.
