#!/usr/bin/env python3
"""Create an auditable, deterministic Things 3 -> Apple Reminders migration plan.

The source Things database is never modified. A consistent SQLite backup is made
first, and every subsequent query runs against that backup.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import hashlib
import json
import os
import plistlib
import secrets
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

SCHEMA = "things-reminders-plan/v4"
SUPPORTED_DB_VERSIONS = {24, 25, 26}
RECURRING_DEADLINE_PLACEHOLDER = 262_213_760
NEXT_INSTANCE_PLACEHOLDER = 69_760
REQUIRED_COLUMNS = {
    "Meta": {"key", "value"},
    "TMArea": {"uuid", "title"},
    "TMTag": {"uuid", "title", "parent", "index"},
    "TMTaskTag": {"tasks", "tags"},
    "TMChecklistItem": {"uuid", "title", "status", "task", "index", "leavesTombstone"},
    "TMTask": {
        "uuid", "type", "status", "trashed", "title", "notes", "start",
        "startDate", "reminderTime", "deadline", "area", "project", "heading",
        "rt1_repeatingTemplate", "rt1_recurrenceRule",
        "rt1_instanceCreationStartDate", "rt1_instanceCreationPaused",
        "rt1_instanceCreationCount", "rt1_afterCompletionReferenceDate",
        "rt1_nextInstanceStartDate", "creationDate", "userModificationDate",
    },
}


class PlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecurrenceParse:
    rules: list[dict[str, Any]]
    original_mode: str
    mode: str
    deadline_offset_days: int | None
    raw: dict[str, Any]
    warnings: list[str]


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def discover_database(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise PlanError(f"Things database not found: {path}")
        return path

    patterns = [
        "~/Library/Group Containers/JLMPQHK86H.com.culturedcode.ThingsMac/ThingsData-*/Things Database.thingsdatabase/main.sqlite",
        "~/Library/Group Containers/JLMPQHK86H.com.culturedcode.ThingsMac/Things Database.thingsdatabase/main.sqlite",
    ]
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(Path(p).resolve() for p in glob.glob(os.path.expanduser(pattern)))
    candidates = sorted({p for p in candidates if p.is_file()}, key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise PlanError("Could not locate Things 3 main.sqlite. Pass --db explicitly.")
    if len(candidates) > 1:
        newest = candidates[0]
        # Refuse close/ambiguous candidates rather than silently choosing the wrong account.
        if newest.stat().st_mtime - candidates[1].stat().st_mtime < 60:
            joined = "\n  ".join(str(p) for p in candidates)
            raise PlanError(f"Multiple plausible Things databases found; pass --db:\n  {joined}")
    return candidates[0]


def open_ro(path: Path) -> sqlite3.Connection:
    quoted = str(path).replace("?", "%3f").replace("#", "%23")
    conn = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def snapshot_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise PlanError(f"Snapshot already exists: {destination}")
    src = open_ro(source)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
        dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dst.commit()
    finally:
        dst.close()
        src.close()


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")')}


def database_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM Meta WHERE key='databaseVersion'").fetchone()
    if not row:
        raise PlanError("Things database has no Meta.databaseVersion")
    value = row[0]
    if isinstance(value, str):
        value = value.encode()
    try:
        parsed = plistlib.loads(value)
        return int(parsed)
    except Exception as exc:
        raise PlanError(f"Could not decode Things databaseVersion: {exc}") from exc


def validate_schema(conn: sqlite3.Connection, allow_unknown: bool) -> int:
    missing: list[str] = []
    for table, required in REQUIRED_COLUMNS.items():
        cols = table_columns(conn, table)
        if not cols:
            missing.append(f"missing table {table}")
            continue
        absent = sorted(required - cols)
        if absent:
            missing.append(f"{table} missing columns: {', '.join(absent)}")
    if missing:
        raise PlanError("Unsupported Things schema:\n- " + "\n- ".join(missing))
    version = database_version(conn)
    if version not in SUPPORTED_DB_VERSIONS and not allow_unknown:
        raise PlanError(
            f"Things databaseVersion={version} is not tested. "
            "Re-run with --allow-unknown-schema only after reviewing the schema diff."
        )
    return version


def decode_packed_date(value: Any) -> dt.date | None:
    if value is None:
        return None
    n = int(value)
    if n in (0, RECURRING_DEADLINE_PLACEHOLDER, NEXT_INSTANCE_PLACEHOLDER):
        return None
    year = n >> 16
    month = (n >> 12) & 0xF
    day = (n >> 7) & 0x1F
    try:
        if year < 1900 or year > 4095:
            return None
        return dt.date(year, month, day)
    except ValueError:
        return None


def decode_reminder_time(value: Any) -> dt.time | None:
    if value is None:
        return None
    n = int(value)
    if n == 0:
        return None
    hour = (n & 2_080_374_784) >> 26
    minute = (n & 66_060_288) >> 20
    try:
        return dt.time(hour=hour, minute=minute)
    except ValueError:
        return None


def date_from_epoch(value: Any) -> dt.date | None:
    if value is None:
        return None
    try:
        return dt.datetime.fromtimestamp(float(value)).date()
    except (ValueError, OSError, OverflowError):
        return None


def iso_date(value: dt.date | None) -> str | None:
    return value.isoformat() if value else None


def iso_local_datetime(day: dt.date | None, clock: dt.time | None) -> str | None:
    if day is None or clock is None:
        return None
    # Deliberately timezone-naive. EventKitBridge interprets it in the Mac's current timezone.
    return dt.datetime.combine(day, clock).isoformat(timespec="minutes")


def int_value(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    return int(value)


def exact_month_day_rule(base: dict[str, Any], day: int) -> dict[str, Any]:
    """Encode one exact calendar day as one EventKit BYMONTHDAY value.

    Apple Reminders does not reliably honor ``setPositions`` for reminders.
    Candidate-range workarounds such as days 1...31 plus BYSETPOS=1 therefore
    expand into a reminder on every listed date.  A fixed month day must remain
    a single explicit value: day 1 becomes ``daysOfMonth=[1]``, day 21 becomes
    ``daysOfMonth=[21]``.
    """
    if day < 1 or day > 31:
        raise PlanError(f"Invalid exact monthly day: {day}")
    rule = dict(base)
    rule["daysOfMonth"] = [day]
    rule["setPositions"] = []
    return rule


def make_monthly_schedule_explicit(
    recurrence: RecurrenceParse | None, due_day: dt.date | None
) -> RecurrenceParse | None:
    """Materialize an implicit monthly anchor as an exact custom rule.

    Things may encode "repeat monthly on the anchor date" with an empty ``of``
    array. Convert that anchor to one explicit ``daysOfMonth`` value so a rule
    anchored on the 21st remains the 21st and never expands to every day.
    """
    if recurrence is None or due_day is None:
        return recurrence

    changed = False
    rules: list[dict[str, Any]] = []
    for original in recurrence.rules:
        rule = dict(original)
        if rule.get("frequency") == "monthly" and not rule.get("daysOfMonth"):
            rule = exact_month_day_rule(rule, due_day.day)
            changed = True
        rules.append(rule)

    if not changed:
        return recurrence
    return RecurrenceParse(
        rules=rules,
        original_mode=recurrence.original_mode,
        mode=recurrence.mode,
        deadline_offset_days=recurrence.deadline_offset_days,
        raw=recurrence.raw,
        warnings=recurrence.warnings,
    )


def parse_recurrence(blob: bytes, after_completion_policy: str) -> RecurrenceParse:
    try:
        raw = plistlib.loads(blob)
    except Exception as exc:
        raise PlanError(f"Cannot decode recurrence plist: {exc}") from exc
    if not isinstance(raw, dict):
        raise PlanError("Recurrence plist is not a dictionary")
    known_keys = {"ed", "fa", "fu", "ia", "of", "rc", "rrv", "sr", "tp", "ts"}
    unknown_keys = sorted(set(map(str, raw.keys())) - known_keys)
    if unknown_keys:
        raise PlanError(f"Unknown recurrence fields: {', '.join(unknown_keys)}")
    rule_version = int_value(raw.get("rrv"), 4)
    if rule_version != 4:
        raise PlanError(f"Unsupported recurrence rule version rrv={rule_version}")

    frequency_code = int_value(raw.get("fu"), -1)
    frequency = {16: "daily", 256: "weekly", 8: "monthly", 4: "yearly"}.get(frequency_code)
    if not frequency:
        raise PlanError(f"Unknown recurrence frequency code fu={frequency_code}")
    interval = int_value(raw.get("fa"), 1)
    if interval < 1:
        raise PlanError(f"Invalid recurrence interval fa={interval}")

    tp = int_value(raw.get("tp"), 0)
    if tp not in (0, 1):
        raise PlanError(f"Unknown recurrence mode tp={tp}")
    original_mode = "after_completion" if tp == 1 else "schedule"
    mode = original_mode
    warnings: list[str] = []
    if original_mode == "after_completion":
        if after_completion_policy == "abort":
            raise PlanError(
                "Things rule repeats after completion, which Apple Reminders/EventKit cannot represent natively. "
                "Use --after-completion=fixed to convert it to a fixed schedule after reviewing the plan."
            )
        mode = "schedule"
        warnings.append("Converted Things 'after completion' recurrence to a fixed Apple Reminders schedule")

    offsets = raw.get("of") or []
    if not isinstance(offsets, list):
        raise PlanError("Recurrence key 'of' is not an array")
    normalized_offsets: list[dict[str, int]] = []
    for item in offsets:
        if not isinstance(item, dict):
            raise PlanError("Recurrence offset is not a dictionary")
        normalized_offsets.append({str(k): int(v) for k, v in item.items()})

    base: dict[str, Any] = {
        "frequency": frequency,
        "interval": interval,
        "weekdays": [],
        "daysOfMonth": [],
        "monthsOfYear": [],
        "setPositions": [],
        "endDate": None,
        "occurrenceCount": None,
    }

    end_epoch = raw.get("ed")
    if end_epoch is not None:
        end_day = date_from_epoch(end_epoch)
        if end_day and end_day.year < 4000:
            base["endDate"] = end_day.isoformat()
    repeat_count = int_value(raw.get("rc"), 0)
    if repeat_count != 0:
        # The meaning of this internal Things field is not documented well enough
        # to map it safely to EventKit's occurrenceCount. Fail closed instead of
        # guessing and accidentally shortening or extending a series.
        raise PlanError(f"Unsupported non-zero recurrence count field rc={repeat_count}")

    rules: list[dict[str, Any]] = []
    if frequency == "daily":
        unknown = [o for o in normalized_offsets if set(o) - {"dy"} or int(o.get("dy", 0)) != 0]
        if unknown:
            raise PlanError(f"Unsupported daily recurrence offsets: {unknown}")
        rules = [base]
    elif frequency == "weekly":
        if any(set(o) != {"wd"} for o in normalized_offsets):
            raise PlanError(f"Unsupported weekly recurrence offsets: {normalized_offsets}")
        weekdays = sorted({o["wd"] + 1 for o in normalized_offsets})
        if any(day < 1 or day > 7 for day in weekdays):
            raise PlanError(f"Invalid weekday values: {weekdays}")
        base["weekdays"] = weekdays
        rules = [base]
    elif frequency == "monthly":
        if any(set(o) != {"dy"} for o in normalized_offsets):
            raise PlanError(
                "Unsupported monthly recurrence (likely nth-weekday or another advanced pattern): "
                f"{normalized_offsets}"
            )

        # Things stores ordinary month days as zero-based values (0 -> day 1)
        # and uses -1 for the last day of the month. Fixed days map exactly to
        # one BYMONTHDAY value. Do not use BYSETPOS candidate-range workarounds:
        # Reminders may ignore setPositions for reminders and create an occurrence
        # on every candidate day.
        raw_days = {o["dy"] for o in normalized_offsets}
        negative_days = sorted(day for day in raw_days if day < 0)
        if negative_days:
            if negative_days == [-1]:
                raise PlanError(
                    "Things rule repeats on the last day of each month. Apple Reminders on this macOS "
                    "does not preserve either BYMONTHDAY=-1 or BYSETPOS-based last-day rules reliably. "
                    "Excluded to prevent an incorrect 31st-only or every-day schedule; keep it in Things "
                    "or materialize the dates manually."
                )
            raise PlanError(
                "Unsupported negative Things monthly day offsets: "
                f"{negative_days}; no reliable Apple Reminders mapping is known"
            )

        positive_days = sorted({day + 1 for day in raw_days})
        if any(day < 1 or day > 31 for day in positive_days):
            raise PlanError(f"Invalid monthly day values: {positive_days}")
        for day in positive_days:
            rules.append(exact_month_day_rule(base, day))

        if not raw_days:
            rules = [base]
    else:  # yearly
        if any(set(o) != {"dy", "mo"} for o in normalized_offsets):
            raise PlanError(f"Unsupported yearly recurrence offsets: {normalized_offsets}")
        # Use one EventKit rule per exact month/day pair to avoid accidental Cartesian products.
        for offset in normalized_offsets or [{}]:
            rule = dict(base)
            if offset:
                month, day = offset["mo"] + 1, offset["dy"] + 1
                if not 1 <= month <= 12 or not 1 <= day <= 31:
                    raise PlanError(f"Invalid yearly offset: {offset}")
                rule["monthsOfYear"] = [month]
                rule["daysOfMonth"] = [day]
            rules.append(rule)

    ts = raw.get("ts")
    # `ts` is an undocumented Things-internal field. Different reverse-engineered
    # clients disagree about its sign and sentinel values. We retain it in `raw`
    # for audit, but never infer a deadline from it without a generated occurrence.
    if ts is not None:
        int(ts)  # validate that the value is numeric
    deadline_offset = None
    return RecurrenceParse(
        rules=rules,
        original_mode=original_mode,
        mode=mode,
        deadline_offset_days=deadline_offset,
        raw={str(k): v for k, v in raw.items() if k != "of"} | {"of": normalized_offsets},
        warnings=warnings,
    )


def fetch_tasks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    has_index = "index" in table_columns(conn, "TMTask")
    item_index = 't."index"' if has_index else "NULL"
    heading_index = 'h."index"' if has_index else "NULL"
    query = f"""
    SELECT
      t.uuid, t.type, t.status, t.trashed, t.title, t.notes, t.start,
      t.startDate, t.reminderTime, t.deadline, t.area, t.project, t.heading,
      t.rt1_repeatingTemplate, t.rt1_recurrenceRule,
      t.rt1_instanceCreationStartDate, t.rt1_instanceCreationPaused,
      t.rt1_instanceCreationCount, t.rt1_afterCompletionReferenceDate,
      t.rt1_nextInstanceStartDate, t.creationDate, t.userModificationDate,
      {item_index} AS item_index,
      a.uuid AS resolved_area_uuid, a.title AS area_title,
      p.title AS project_title, h.title AS heading_title,
      h.creationDate AS heading_creation_date,
      {heading_index} AS heading_index
    FROM TMTask t
    LEFT JOIN TMTask h ON h.uuid = t.heading
    LEFT JOIN TMTask p ON p.uuid = COALESCE(t.project, h.project)
    LEFT JOIN TMArea a ON a.uuid = COALESCE(t.area, p.area)
    ORDER BY t.creationDate, t.uuid
    """
    return [dict(row) for row in conn.execute(query)]


def fetch_tags(conn: sqlite3.Connection) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    query = """
      SELECT l.tasks AS task_uuid, tag.title AS title
      FROM TMTaskTag l JOIN TMTag tag ON tag.uuid = l.tags
      ORDER BY tag.parent IS NOT NULL, tag."index", tag.title
    """
    for row in conn.execute(query):
        if row["title"]:
            result.setdefault(row["task_uuid"], []).append(str(row["title"]))
    return result


def fetch_checklists(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    query = """
      SELECT task, uuid, title, status, "index"
      FROM TMChecklistItem
      WHERE IFNULL(leavesTombstone, 0) = 0
      ORDER BY task, "index", uuid
    """
    for row in conn.execute(query):
        result.setdefault(row["task"], []).append({
            "uuid": row["uuid"],
            "title": row["title"] or "",
            "completed": int_value(row["status"]) == 3,
        })
    return result


def sanitize_component(value: str, fallback: str) -> str:
    cleaned = " ".join(value.replace("\n", " ").replace("\r", " ").split()).strip()
    return (cleaned or fallback)[:100]


def escaped_note_lines(value: str) -> list[str]:
    return value.rstrip().splitlines()


def note_only_url(value: str) -> str | None:
    """Return a web URL when the entire note is only that URL."""
    candidate = value.strip()
    if not candidate or any(ch.isspace() for ch in candidate):
        return None
    parsed = urlparse(candidate)
    if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
        return candidate
    return None

def choose_current_instance(template: dict[str, Any], instances: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[str]]:
    open_instances = [r for r in instances if int_value(r["status"]) == 0 and int_value(r["trashed"]) == 0]
    warnings: list[str] = []
    if not open_instances:
        return None, warnings
    today = dt.date.today()
    def key(row: dict[str, Any]) -> tuple[int, int, float]:
        start = decode_packed_date(row.get("startDate"))
        due = decode_packed_date(row.get("deadline"))
        day = start or due
        if day is None:
            bucket, ordinal = 2, 10**9
        elif day >= today:
            bucket, ordinal = 0, day.toordinal()
        else:
            bucket, ordinal = 1, -day.toordinal()
        return bucket, ordinal, -float(row.get("userModificationDate") or 0)
    open_instances.sort(key=key)
    if len(open_instances) > 1:
        warnings.append(
            f"Template has {len(open_instances)} open generated instances; selected {open_instances[0]['uuid']} deterministically"
        )
    return open_instances[0], warnings


def combine_notes(
    title_context: dict[str, str | None],
    original_notes: str,
    tags: list[str],
    checklist: list[dict[str, Any]],
    extra_sections: list[str] | None = None,
) -> tuple[str, str | None]:
    """Build concise Reminders notes and extract a note-only web URL."""
    blocks: list[str] = []
    extracted_url = note_only_url(original_notes)
    if original_notes.strip() and extracted_url is None:
        blocks.append("\n".join(escaped_note_lines(original_notes)))

    context: list[str] = []
    # Area is intentionally omitted: the destination list already represents it.
    for label, key in (("Project", "project"), ("Heading", "heading")):
        value = title_context.get(key)
        if value:
            context.append(f"{label}: {sanitize_component(str(value), label)}")
    if tags:
        context.append("Tags: " + " ".join(f"#{sanitize_component(str(t), 'tag')}" for t in tags))
    if checklist:
        context.append("Checklist:\n" + "\n".join(
            f"- [{'x' if x['completed'] else ' '}] {sanitize_component(str(x['title']), 'Untitled checklist item')}"
            for x in checklist
        ))
    for section in extra_sections or []:
        if section.strip():
            context.append(section.strip())
    if context:
        blocks.append("\n".join(context))
    return "\n\n".join(blocks).strip(), extracted_url

def project_children(
    project_id: str,
    rows: list[dict[str, Any]],
    row_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return non-trashed child to-dos in stable Things order.

    Children may point directly at the project or indirectly through a heading.
    Headings themselves are represented through each child's resolved heading title.
    """
    children: list[dict[str, Any]] = []
    for row in rows:
        if int_value(row.get("type")) != 0 or int_value(row.get("trashed")) != 0:
            continue
        parent = row.get("project")
        if not parent and row.get("heading"):
            heading = row_by_id.get(str(row["heading"]))
            parent = heading.get("project") if heading else None
        if str(parent or "") == project_id:
            children.append(row)
    children.sort(key=lambda row: (
        int_value(row.get("heading_index") if row.get("heading") else row.get("item_index"), 0),
        float(row.get("heading_creation_date") or row.get("creationDate") or 0),
        int_value(row.get("item_index"), 0),
        float(row.get("creationDate") or 0),
        str(row.get("uuid")),
    ))
    return children


def project_outline(
    project_id: str,
    rows: list[dict[str, Any]],
    row_by_id: dict[str, dict[str, Any]],
    tags_by_task: dict[str, list[str]],
    checklist_by_task: dict[str, list[dict[str, Any]]],
) -> str | None:
    """Build a Markdown blueprint of a recurring project's child tasks."""
    children = [row for row in project_children(project_id, rows, row_by_id) if int_value(row.get("status")) != 2]
    if not children:
        return None
    lines = ["Project blueprint:"]
    current_heading: str | None = None
    for row in children:
        heading = sanitize_component(str(row.get("heading_title") or ""), "") or None
        if heading != current_heading:
            current_heading = heading
            if heading:
                lines.append(f"### {heading}")
        title = sanitize_component(str(row.get("title") or "Untitled"), "Untitled")
        indent = "  " if heading else ""
        lines.append(f"{indent}- [ ] {title}")
        detail_indent = indent + "  "
        notes = str(row.get("notes") or "").strip()
        child_url = note_only_url(notes)
        if child_url:
            lines.append(f"{detail_indent}- Link: {child_url}")
        elif notes:
            lines.extend(f"{detail_indent}{line}" for line in escaped_note_lines(notes))
        tags = tags_by_task.get(str(row["uuid"]), [])
        if tags:
            lines.append(detail_indent + "Tags: " + " ".join(
                f"#{sanitize_component(str(tag), 'tag')}" for tag in tags
            ))
        checklist = checklist_by_task.get(str(row["uuid"]), [])
        for entry in checklist:
            lines.append(
                f"{detail_indent}- [{'x' if entry['completed'] else ' '}] "
                f"{sanitize_component(str(entry['title']), 'Untitled checklist item')}"
            )
    return "\n".join(lines)


STATUS_NAMES = {0: "open", 2: "canceled", 3: "completed"}


def context_state(row: dict[str, Any]) -> str:
    if int_value(row.get("trashed")) != 0:
        return "trashed"
    return STATUS_NAMES.get(int_value(row.get("status")), f"status={int_value(row.get('status'))}")


def resolved_parent_project_id(
    row: dict[str, Any],
    row_by_id: dict[str, dict[str, Any]],
) -> tuple[str | None, str | None]:
    """Resolve a task's project through either a direct project or a heading.

    Returns ``(project_id, structural_error)``. Things normally keeps only one
    route, but migration must fail closed when stale or inconsistent references
    are present in the database.
    """
    direct = str(row.get("project") or "") or None
    heading_id = str(row.get("heading") or "") or None
    if not heading_id:
        return direct, None

    heading = row_by_id.get(heading_id)
    if heading is None:
        return None, f"Referenced heading [{heading_id}] is missing"
    if int_value(heading.get("type")) != 2:
        return None, f"Referenced heading [{heading_id}] has unexpected type={int_value(heading.get('type'))}"

    via_heading = str(heading.get("project") or "") or None
    if direct and via_heading and direct != via_heading:
        return None, f"Task has inconsistent direct project [{direct}] and heading project [{via_heading}]"
    return via_heading or direct, None


def inactive_context_reason(
    row: dict[str, Any],
    row_by_id: dict[str, dict[str, Any]],
) -> str | None:
    """Return why a row is not visible in an active Things context.

    Things may leave child to-dos with ``status=0`` after their heading/project
    has been completed, canceled, or moved to Trash. Filtering only the child
    status therefore leaks Logbook tasks into an export.
    """
    heading_id = str(row.get("heading") or "") or None
    if heading_id:
        heading = row_by_id.get(heading_id)
        if heading is None:
            return f"Referenced heading [{heading_id}] is missing"
        state = context_state(heading)
        if state != "open":
            title = sanitize_component(str(heading.get("title") or "Untitled heading"), "Untitled heading")
            return f"Parent heading '{title}' is {state}"

    project_id, structural_error = resolved_parent_project_id(row, row_by_id)
    if structural_error:
        return structural_error
    if project_id:
        project = row_by_id.get(project_id)
        if project is None:
            return f"Referenced project [{project_id}] is missing"
        if int_value(project.get("type")) != 1:
            return f"Referenced project [{project_id}] has unexpected type={int_value(project.get('type'))}"
        state = context_state(project)
        if state != "open":
            title = sanitize_component(str(project.get("title") or "Untitled project"), "Untitled project")
            return f"Parent project '{title}' is {state}"
    return None


def build_plan(
    snapshot: Path,
    run_id: str,
    list_prefix: str,
    after_completion_policy: str,
    allow_unknown_schema: bool,
    unsupported_policy: str = "abort",
    recurring_project_policy: str = "summary",
) -> tuple[dict[str, Any], str, bool]:
    conn = open_ro(snapshot)
    try:
        version = validate_schema(conn, allow_unknown_schema)
        rows = fetch_tasks(conn)
        tags_by_task = fetch_tags(conn)
        checklist_by_task = fetch_checklists(conn)
    finally:
        conn.close()

    row_by_id = {r["uuid"]: r for r in rows}

    # Keep distinct Things Areas distinct even when they share a title, or when an
    # Area itself is named "Inbox". Add a short identity suffix only when needed.
    identities_by_leaf: dict[str, set[str]] = {}
    for row in rows:
        leaf = sanitize_component(str(row.get("area_title") or "Inbox"), "Inbox")
        identity = str(row.get("resolved_area_uuid") or "__inbox__")
        identities_by_leaf.setdefault(leaf.casefold(), set()).add(identity)

    def list_leaf_for(*candidates: dict[str, Any] | None) -> str:
        row = next((candidate for candidate in candidates if candidate), {})
        title = sanitize_component(str(row.get("area_title") or "Inbox"), "Inbox")
        identity = str(row.get("resolved_area_uuid") or "__inbox__")
        if len(identities_by_leaf.get(title.casefold(), set())) > 1:
            suffix = "inbox" if identity == "__inbox__" else sanitize_component(identity, "area")[:8]
            return f"{title} [{suffix}]"
        return title

    instances_by_template: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        template_id = row.get("rt1_repeatingTemplate")
        if template_id:
            instances_by_template.setdefault(str(template_id), []).append(row)

    items: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    excluded_context: list[dict[str, Any]] = []
    global_warnings: list[str] = []

    def add_unsupported(
        row: dict[str, Any],
        source_id: str,
        title: str,
        reason: str,
        *,
        extra_sections: list[str] | None = None,
    ) -> None:
        area = row.get("area_title")
        project = row.get("project_title")
        heading = row.get("heading_title")
        list_leaf = list_leaf_for(row)
        source_checklist = checklist_by_task.get(source_id, [])
        source_tags = tags_by_task.get(source_id, [])
        content, note_url = combine_notes(
            title_context={"project": project, "heading": heading},
            original_notes=str(row.get("notes") or ""),
            tags=source_tags,
            checklist=source_checklist,
            extra_sections=extra_sections,
        )
        unsupported.append({
            "sourceID": source_id,
            "title": title,
            "reason": reason,
            "type": {0: "todo", 1: "project", 2: "heading"}.get(int_value(row.get("type")), "unknown"),
            "url": f"things:///show?id={source_id}",
            "listTitle": f"{list_prefix} — {list_leaf}",
            "startDate": iso_date(decode_packed_date(row.get("startDate"))),
            "deadline": iso_date(decode_packed_date(row.get("deadline"))),
            "content": content,
            "noteURL": note_url,
        })

    def add_context_excluded(row: dict[str, Any], reason: str, source_id: str | None = None) -> None:
        item_id = source_id or str(row.get("uuid") or "")
        excluded_context.append({
            "sourceID": item_id,
            "title": sanitize_component(str(row.get("title") or "Untitled"), "Untitled"),
            "reason": reason,
            "url": f"things:///show?id={item_id}",
        })

    def make_item(
        row: dict[str, Any],
        template: dict[str, Any] | None = None,
        *,
        extra_sections: list[str] | None = None,
        additional_warnings: list[str] | None = None,
    ) -> None:
        source_id = str((template or row)["uuid"])
        effective = row
        recurrence: RecurrenceParse | None = None
        item_warnings: list[str] = list(additional_warnings or [])
        if template is not None:
            try:
                recurrence = parse_recurrence(bytes(template["rt1_recurrenceRule"]), after_completion_policy)
                item_warnings.extend(recurrence.warnings)
            except Exception as exc:
                add_unsupported(
                    template, source_id, template.get("title") or "", str(exc),
                    extra_sections=extra_sections,
                )
                return

        start_day = decode_packed_date(effective.get("startDate"))
        deadline_day = decode_packed_date(effective.get("deadline"))
        if template is not None and start_day is None:
            start_day = (
                decode_packed_date(template.get("rt1_nextInstanceStartDate"))
                or decode_packed_date(template.get("rt1_instanceCreationStartDate"))
                or date_from_epoch(recurrence.raw.get("ia") if recurrence else None)
                or date_from_epoch(recurrence.raw.get("sr") if recurrence else None)
            )
        using_generated_instance = (
            template is not None and str(effective.get("uuid")) != str(template.get("uuid"))
        )
        if (
            template is not None
            and not using_generated_instance
            and int_value(template.get("deadline")) == RECURRING_DEADLINE_PLACEHOLDER
        ):
            add_unsupported(
                template, source_id, template.get("title") or "",
                "Recurring template has a relative deadline but no open generated occurrence to verify the exact date relationship",
                extra_sections=extra_sections,
            )
            return

        if template is not None and start_day is None and deadline_day is None:
            add_unsupported(
                template, source_id, template.get("title") or "",
                "Could not determine the first occurrence date for recurring template",
                extra_sections=extra_sections,
            )
            return

        if recurrence and start_day and deadline_day:
            observed_offset = (deadline_day - start_day).days
            if observed_offset != 0:
                frequencies = {rule["frequency"] for rule in recurrence.rules}
                if not frequencies.issubset({"daily", "weekly"}):
                    add_unsupported(
                        template or effective, source_id, (template or effective).get("title") or "",
                        "Recurring task has a deadline offset from its start date; exact monthly/yearly mapping is not possible in Apple Reminders",
                        extra_sections=extra_sections,
                    )
                    return
                shifted_rules: list[dict[str, Any]] = []
                for original in recurrence.rules:
                    rule = dict(original)
                    if rule["frequency"] == "weekly" and rule.get("weekdays"):
                        rule["weekdays"] = sorted({((int(day) - 1 + observed_offset) % 7) + 1 for day in rule["weekdays"]})
                    if rule.get("endDate"):
                        rule["endDate"] = (dt.date.fromisoformat(rule["endDate"]) + dt.timedelta(days=observed_offset)).isoformat()
                    shifted_rules.append(rule)
                recurrence = RecurrenceParse(
                    rules=shifted_rules,
                    original_mode=recurrence.original_mode,
                    mode=recurrence.mode,
                    deadline_offset_days=observed_offset,
                    raw=recurrence.raw,
                    warnings=recurrence.warnings + [
                        f"Shifted native recurrence by {observed_offset:+d} day(s) to preserve the recurring Things deadline"
                    ],
                )
                item_warnings = list(additional_warnings or []) + list(recurrence.warnings)

        reminder_clock = decode_reminder_time(effective.get("reminderTime"))
        if reminder_clock is None and template is not None:
            reminder_clock = decode_reminder_time(template.get("reminderTime"))

        title_source = effective.get("title") or (template or {}).get("title") or "Untitled"
        title = sanitize_component(str(title_source), "Untitled")
        area = effective.get("area_title") or (template or {}).get("area_title")
        project = effective.get("project_title") or (template or {}).get("project_title")
        heading = effective.get("heading_title") or (template or {}).get("heading_title")
        list_leaf = list_leaf_for(effective, template)
        list_title = f"{list_prefix} — {list_leaf}"

        source_tags = sorted(set(tags_by_task.get(source_id, []) + tags_by_task.get(str(effective["uuid"]), [])))
        source_checklist = checklist_by_task.get(str(effective["uuid"])) or checklist_by_task.get(source_id, [])
        notes, note_url = combine_notes(
            title_context={"project": project, "heading": heading},
            original_notes=str(effective.get("notes") or (template or {}).get("notes") or ""),
            tags=source_tags,
            checklist=source_checklist,
            extra_sections=extra_sections,
        )
        due_day = deadline_day or start_day
        recurrence = make_monthly_schedule_explicit(recurrence, due_day)
        item = {
            "sourceID": source_id,
            "sourceInstanceID": str(effective["uuid"]),
            "title": title,
            "notes": notes,
            "url": note_url,
            "listTitle": list_title,
            "dueDate": iso_date(due_day),
            "alarmDate": iso_local_datetime(start_day or due_day, reminder_clock),
            "priority": 0,
            "recurrenceRules": recurrence.rules if recurrence else [],
            "recurrenceOriginalMode": recurrence.original_mode if recurrence else None,
            "warnings": item_warnings,
        }
        items.append(item)

    # Normal, open, non-generated todos. Children of recurring projects are
    # excluded rather than flattened into independent permanent reminders.
    recurring_project_ids = {
        str(r["uuid"]) for r in rows
        if int_value(r["type"]) == 1 and (r.get("rt1_recurrenceRule") or r.get("rt1_repeatingTemplate"))
    }
    for row in rows:
        if int_value(row["type"]) != 0 or int_value(row["status"]) != 0 or int_value(row["trashed"]) != 0:
            continue
        if row.get("rt1_repeatingTemplate") or row.get("rt1_recurrenceRule"):
            continue
        context_reason = inactive_context_reason(row, row_by_id)
        if context_reason:
            add_context_excluded(row, context_reason)
            continue
        parent_project, structural_error = resolved_parent_project_id(row, row_by_id)
        if structural_error:
            add_context_excluded(row, structural_error)
            continue
        if parent_project and str(parent_project) in recurring_project_ids:
            continue
        make_item(row)

    # Recurring todo templates become one native recurring reminder. Generated instances are not imported separately.
    for template in rows:
        if int_value(template["type"]) != 0 or int_value(template["status"]) != 0 or int_value(template["trashed"]) != 0:
            continue
        if not template.get("rt1_recurrenceRule"):
            continue
        context_reason = inactive_context_reason(template, row_by_id)
        if context_reason:
            add_context_excluded(template, context_reason)
            continue
        parent_project, structural_error = resolved_parent_project_id(template, row_by_id)
        if structural_error:
            add_context_excluded(template, structural_error)
            continue
        if parent_project and str(parent_project) in recurring_project_ids:
            # The recurring-project summary owns this subtree. Importing a nested
            # recurring task separately would duplicate it outside the project.
            continue
        if int_value(template.get("rt1_instanceCreationPaused")) == 1:
            add_unsupported(
                template, str(template["uuid"]), template.get("title") or "",
                "Recurring template is paused in Things; importing it as active would change its state",
            )
            continue
        active_instances = [
            candidate for candidate in instances_by_template.get(str(template["uuid"]), [])
            if inactive_context_reason(candidate, row_by_id) is None
        ]
        current, warnings = choose_current_instance(template, active_instances)
        global_warnings.extend(f"{template['uuid']}: {w}" for w in warnings)
        make_item(current or template, template=template)

    if recurring_project_policy not in {"summary", "manual"}:
        raise PlanError(f"Unknown recurring-project policy: {recurring_project_policy}")

    # A recurring Things project generates a fresh project subtree. EventKit can
    # repeat a reminder, but it cannot repeat a list/subtree. In summary mode we
    # preserve the native recurrence on one reminder and embed the project template
    # as a static, auditable outline. Manual mode retains the old fail-closed path.
    for template in rows:
        if (
            int_value(template["type"]) != 1
            or int_value(template["status"]) != 0
            or int_value(template["trashed"]) != 0
            or not template.get("rt1_recurrenceRule")
        ):
            continue
        source_id = str(template["uuid"])
        outline = project_outline(source_id, rows, row_by_id, tags_by_task, checklist_by_task)
        current, warnings = choose_current_instance(template, instances_by_template.get(source_id, []))
        global_warnings.extend(f"{source_id}: {warning}" for warning in warnings)
        if outline is None and current is not None:
            outline = project_outline(str(current["uuid"]), rows, row_by_id, tags_by_task, checklist_by_task)
        if recurring_project_policy == "manual":
            add_unsupported(
                template, source_id, template.get("title") or "",
                "Recurring project was left for manual transfer by --recurring-projects=manual",
                extra_sections=[outline] if outline else None,
            )
            continue
        if int_value(template.get("rt1_instanceCreationPaused")) == 1:
            add_unsupported(
                template, source_id, template.get("title") or "",
                "Recurring project template is paused in Things; importing it as active would change its state",
                extra_sections=[outline] if outline else None,
            )
            continue
        make_item(
            current or template,
            template=template,
            extra_sections=[outline] if outline else None,
            additional_warnings=[
                "Recurring project imported as one recurring reminder; its child tasks are preserved as a static outline in notes, not as independently completable repeating reminders"
            ],
        )

    items.sort(key=lambda x: (x["listTitle"].casefold(), x["dueDate"] or "9999", x["title"].casefold(), x["sourceID"]))
    calendars = sorted({item["listTitle"] for item in items}, key=str.casefold)
    if unsupported_policy not in {"abort", "manual"}:
        raise PlanError(f"Unknown unsupported policy: {unsupported_policy}")
    blocked = bool(unsupported) and unsupported_policy == "abort"
    plan = {
        "schema": SCHEMA,
        "runID": run_id,
        "createdAt": now_utc(),
        "databaseVersion": version,
        "afterCompletionPolicy": after_completion_policy,
        "unsupportedPolicy": unsupported_policy,
        "recurringProjectPolicy": recurring_project_policy,
        "listPrefix": list_prefix,
        "calendarTitles": calendars,
        "items": items,
        "unsupported": unsupported,
        "excludedInactiveContext": excluded_context,
        "warnings": global_warnings,
        "blocked": blocked,
    }
    report_lines = [
        f"Run ID: {run_id}",
        f"Things databaseVersion: {version}",
        f"Planned reminders: {len(items)}",
        f"Planned lists: {len(calendars)}",
        f"Recurring reminders: {sum(bool(i['recurrenceRules']) for i in items)}",
        f"Unsupported/blocking items: {len(unsupported)}",
        f"Inactive-context items excluded: {len(excluded_context)}",
        f"After-completion policy: {after_completion_policy}",
        f"Unsupported-item policy: {unsupported_policy}",
        f"Recurring-project policy: {recurring_project_policy}",
        "",
    ]
    if unsupported:
        report_lines.append("BLOCKING ITEMS" if blocked else "MANUAL ITEMS (excluded from automatic import)")
        report_lines.extend(f"- {x['title']} [{x['sourceID']}]: {x['reason']}" for x in unsupported)
        report_lines.append("")
    if excluded_context:
        report_lines.append("INACTIVE-CONTEXT ITEMS (excluded from automatic import)")
        report_lines.extend(f"- {x['title']} [{x['sourceID']}]: {x['reason']}" for x in excluded_context)
        report_lines.append("")
    if global_warnings or any(i["warnings"] for i in items):
        report_lines.append("WARNINGS")
        report_lines.extend(f"- {w}" for w in global_warnings)
        for item in items:
            report_lines.extend(f"- {item['title']} [{item['sourceID']}]: {w}" for w in item["warnings"])
        report_lines.append("")
    report_lines.append("No Apple Reminders data has been changed. Review plan.json before apply.")
    return plan, "\n".join(report_lines) + "\n", blocked


def default_run_dir(run_id: str) -> Path:
    return Path.home() / "Documents" / "Things Reminders Migration" / run_id


def write_review_files(run_dir: Path, plan: dict[str, Any]) -> None:
    csv_path = run_dir / "plan.csv"
    fd, tmp_name = tempfile.mkstemp(prefix=".plan.csv.", dir=run_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["source_id", "title", "list", "due_date", "alarm_date", "recurring", "original_mode", "warnings"])
            for item in plan["items"]:
                writer.writerow([
                    item["sourceID"], item["title"], item["listTitle"], item["dueDate"] or "",
                    item["alarmDate"] or "", "yes" if item["recurrenceRules"] else "no",
                    item["recurrenceOriginalMode"] or "", " | ".join(item["warnings"]),
                ])
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp_name, csv_path)
    except Exception:
        try: os.unlink(tmp_name)
        except FileNotFoundError: pass
        raise

    review_lines = [
        "# Manual review — Things → Apple Reminders",
        "",
        f"Run ID: `{plan['runID']}`",
        "",
        "> Do not delete or complete the originals in Things until automatic verification succeeds and every unchecked item below is handled.",
        "",
    ]
    if plan["unsupported"]:
        review_lines += [
            "## Items excluded from automatic import",
            "",
            "These items were excluded rather than approximated silently.",
            "",
        ]
        for item in plan["unsupported"]:
            review_lines += [
                f"- [ ] **{item['title']}** (`{item.get('type', 'unknown')}`)",
                f"  - Reason: {item['reason']}",
                f"  - Open in Things: {item.get('url', 'things:///show?id=' + item['sourceID'])}",
                f"  - Suggested Reminders list: `{item.get('listTitle', plan['listPrefix'])}`",
            ]
            if item.get("startDate"):
                review_lines.append(f"  - Things start date: {item['startDate']}")
            if item.get("deadline"):
                review_lines.append(f"  - Things deadline: {item['deadline']}")
            review_lines += [
                "  - Create it manually in Reminders and reproduce the Repeat setting shown in Things.",
                "  - For **After Completion**, Reminders has no exact native equivalent: keep it in Things, choose an explicitly accepted fixed schedule, or create a completion-triggered Shortcut.",
            ]
            if item.get("noteURL"):
                review_lines.append(f"  - URL from Things note: {item['noteURL']}")
            if item.get("content"):
                review_lines += ["  - Content to copy:", "", "```text", item["content"], "```", ""]
    else:
        review_lines += ["No items were excluded from automatic import.", ""]

    if plan.get("excludedInactiveContext"):
        review_lines += [
            "## Intentionally excluded inactive-context items",
            "",
            "These to-dos are individually open in SQLite but belong to a completed, canceled, trashed, or structurally invalid parent context. They are normally Logbook/archived data and were not imported.",
            "",
        ]
        for item in plan["excludedInactiveContext"]:
            review_lines += [
                f"- **{item['title']}** — {item['reason']}",
                f"  - Open in Things: {item['url']}",
            ]
        review_lines.append("")

    review_lines += [
        "## Before finishing",
        "",
        "- [ ] Run `./things-reminders verify RUN_DIR`.",
        "- [ ] Spot-check dates, alerts, notes, and at least one occurrence of each imported repeat pattern.",
        "- [ ] Complete every manual item above.",
        "- [ ] Keep the run directory until you are certain the migration is accepted.",
        "",
    ]
    atomic_write(run_dir / "MANUAL_REVIEW.md", ("\n".join(review_lines) + "\n").encode())

    manual = f"""MANUAL ROLLBACK — RUN {plan['runID']}

Preferred:
  ./things-reminders rollback "{run_dir}"

Without the tool:
1. Open `ROLLBACK_INVENTORY.txt` after apply. It records every exact EventKit reminder and list ID.
2. In Apple Reminders, locate the isolated lists whose names start with:
   {plan['listPrefix']}
3. Delete those lists only after confirming they contain no reminders you added after migration.
4. If an imported reminder was moved elsewhere, use its exact open URL from `ROLLBACK_INVENTORY.txt` and delete that reminder manually.

No migration metadata is embedded in reminder notes. Keep this run directory until the migration is accepted.
"""
    atomic_write(run_dir / "MANUAL_ROLLBACK.txt", manual.encode())


def cmd_plan(args: argparse.Namespace) -> int:
    source = discover_database(args.db)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = args.run_id or f"{stamp}-{secrets.token_hex(3)}"
    run_dir = Path(args.run_dir).expanduser().resolve() if args.run_dir else default_run_dir(run_id)
    if run_dir.exists() and any(run_dir.iterdir()):
        raise PlanError(f"Run directory is not empty: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    snapshot = run_dir / "source.sqlite"
    snapshot_database(source, snapshot)
    source_info = {
        "originalPath": str(source),
        "snapshotPath": str(snapshot),
        "snapshotSHA256": sha256(snapshot),
        "snapshotCreatedAt": now_utc(),
    }
    atomic_json(run_dir / "source.json", source_info)

    list_prefix = args.list_prefix or f"Things Import {run_id}"
    plan, report, blocked = build_plan(
        snapshot=snapshot,
        run_id=run_id,
        list_prefix=list_prefix,
        after_completion_policy=args.after_completion,
        allow_unknown_schema=args.allow_unknown_schema,
        unsupported_policy=args.unsupported,
        recurring_project_policy=args.recurring_projects,
    )
    atomic_json(run_dir / "plan.json", plan)
    atomic_write(run_dir / "report.txt", report.encode())
    write_review_files(run_dir, plan)
    atomic_json(run_dir / "state.json", {
        "schema": "things-reminders-state/v1",
        "runID": run_id,
        "status": "blocked" if blocked else "planned",
        "updatedAt": now_utc(),
    })
    print(report, end="")
    print(f"Run directory: {run_dir}")
    return 2 if blocked else 0


def cmd_inspect(args: argparse.Namespace) -> int:
    source = discover_database(args.db)
    with open_ro(source) as conn:
        version = validate_schema(conn, args.allow_unknown_schema)
        counts = {
            "openTodos": conn.execute("SELECT count(*) FROM TMTask WHERE type=0 AND status=0 AND trashed=0").fetchone()[0],
            "recurringTemplates": conn.execute("SELECT count(*) FROM TMTask WHERE type=0 AND status=0 AND trashed=0 AND rt1_recurrenceRule IS NOT NULL").fetchone()[0],
            "generatedInstances": conn.execute("SELECT count(*) FROM TMTask WHERE type=0 AND status=0 AND trashed=0 AND rt1_repeatingTemplate IS NOT NULL").fetchone()[0],
            "recurringProjects": conn.execute("SELECT count(*) FROM TMTask WHERE type=1 AND status=0 AND trashed=0 AND rt1_recurrenceRule IS NOT NULL").fetchone()[0],
        }
    print(json.dumps({"database": str(source), "databaseVersion": version, **counts}, ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", help="Read-only schema and recurrence counters")
    inspect.add_argument("--db")
    inspect.add_argument("--allow-unknown-schema", action="store_true")
    inspect.set_defaults(func=cmd_inspect)

    plan = sub.add_parser("plan", help="Create immutable snapshot and auditable migration plan")
    plan.add_argument("--db")
    plan.add_argument("--run-dir")
    plan.add_argument("--run-id")
    plan.add_argument("--list-prefix")
    plan.add_argument("--after-completion", choices=("abort", "fixed"), default="abort")
    plan.add_argument(
        "--unsupported", choices=("abort", "manual"), default="abort",
        help="abort the whole plan, or exclude unsupported items and list them for manual transfer",
    )
    plan.add_argument(
        "--recurring-projects", choices=("summary", "manual"), default="summary",
        help=(
            "summary creates one native recurring reminder per Things project and preserves child tasks "
            "as a static outline; manual excludes recurring projects for hand transfer"
        ),
    )
    plan.add_argument("--allow-unknown-schema", action="store_true")
    plan.set_defaults(func=cmd_plan)
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        return int(args.func(args))
    except PlanError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; no Reminders changes were made by the planner.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
