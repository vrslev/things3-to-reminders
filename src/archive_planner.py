#!/usr/bin/env python3
"""Create a reversible Apple Reminders plan from a Things verbatim knowledge index.

This command writes only a local run directory. It never opens or modifies Apple
Reminders; the existing preflight/apply/verify/rollback commands consume its plan.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import planner

INDEX_SCHEMA = "things-verbatim-knowledge-index/v1"
PLAN_SCHEMA = "things-reminders-plan/v5"
ALLOWED_CATEGORIES = {
    "decision", "insight", "registered_fact", "dated_event_fact",
}
ALLOWED_TIERS = {"human_keep", "strong", "broad"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def read_index(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        document = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise planner.PlanError(f"Cannot read knowledge index {path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema") != INDEX_SCHEMA:
        raise planner.PlanError(f"Unsupported knowledge-index schema: {document.get('schema') if isinstance(document, dict) else None}")
    source_hash = document.get("source_sha256")
    if not isinstance(source_hash, str) or not SHA256_RE.fullmatch(source_hash):
        raise planner.PlanError("Knowledge index has no valid source_sha256")
    records = document.get("records")
    if not isinstance(records, list) or not records:
        raise planner.PlanError("Knowledge index has no records")

    seen: set[str] = set()
    for position, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise planner.PlanError(f"Knowledge-index record {position} is not an object")
        event_id = record.get("event_id")
        if not isinstance(event_id, str) or not event_id or event_id in seen:
            raise planner.PlanError(f"Invalid or duplicate event_id at record {position}: {event_id!r}")
        seen.add(event_id)
        for field in ("title", "notes", "checklist", "status", "date", "tier", "things_url"):
            if not isinstance(record.get(field), str):
                raise planner.PlanError(f"Record {event_id} has invalid {field}")
        if record["things_url"] != f"things:///show?id={event_id}":
            raise planner.PlanError(f"Record {event_id} has a mismatched Things URL")
        context = record.get("context")
        if not isinstance(context, list) or any(not isinstance(value, str) for value in context):
            raise planner.PlanError(f"Record {event_id} has invalid context")
        categories = record.get("categories")
        if (
            not isinstance(categories, list)
            or not categories
            or any(not isinstance(value, str) or value not in ALLOWED_CATEGORIES for value in categories)
            or len(categories) != len(set(categories))
        ):
            raise planner.PlanError(f"Record {event_id} has invalid categories")
        if record["tier"] not in ALLOWED_TIERS:
            raise planner.PlanError(f"Record {event_id} has invalid tier: {record['tier']}")
        for field in ("area_id", "area", "project", "heading"):
            if field in record and record[field] is not None and not isinstance(record[field], str):
                raise planner.PlanError(f"Record {event_id} has invalid {field}")
        if "tags" in record and (
            not isinstance(record["tags"], list)
            or any(not isinstance(value, str) for value in record["tags"])
        ):
            raise planner.PlanError(f"Record {event_id} has invalid tags")
        if "checklist_items" in record and (
            not isinstance(record["checklist_items"], list)
            or any(
                not isinstance(value, dict)
                or not isinstance(value.get("title"), str)
                or not isinstance(value.get("status"), str)
                for value in record["checklist_items"]
            )
        ):
            raise planner.PlanError(f"Record {event_id} has invalid checklist_items")
        normalize_timestamp(record["date"], event_id)
    return document, raw


def parse_timestamp(value: str, event_id: str = "record") -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise planner.PlanError(f"Record {event_id} has invalid archive date: {value}") from exc
    if parsed.tzinfo is None:
        raise planner.PlanError(f"Record {event_id} archive date has no timezone: {value}")
    return parsed


def normalize_timestamp(value: str, event_id: str = "record") -> str:
    return parse_timestamp(value, event_id).astimezone(dt.timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def local_archive_date(value: str, event_id: str = "record") -> str:
    return parse_timestamp(value, event_id).astimezone().date().isoformat()


def record_hash(source_hash: str, event_id: str) -> str:
    return hashlib.sha256(f"{source_hash}:{event_id}".encode()).hexdigest()


def select_records(document: dict[str, Any], count: int | None) -> tuple[list[dict[str, Any]], str]:
    records = list(document["records"])
    if count is None or count >= len(records):
        return records, "all records"
    if count < 1:
        raise planner.PlanError("--count must be positive")

    source_hash = str(document["source_sha256"])
    selected: dict[str, dict[str, Any]] = {}

    def add(record: dict[str, Any]) -> None:
        if len(selected) < count:
            selected.setdefault(record["event_id"], record)

    # Keep human decisions in every reasonably sized pilot.
    for record in records:
        if record["tier"] == "human_keep":
            add(record)

    # Exercise mappings that are most likely to expose EventKit limits.
    edge_records: list[dict[str, Any]] = []
    empty_titles = [record for record in records if not record["title"]]
    canceled = [record for record in records if record["status"] == "canceled"]
    if empty_titles:
        edge_records.append(empty_titles[0])
    if canceled:
        edge_records.append(canceled[0])
    edge_records.extend([
        max(records, key=lambda record: len(record["title"])),
        max(records, key=lambda record: len(record["notes"]) + len(record["checklist"])),
    ])
    for category in sorted(ALLOWED_CATEGORIES):
        match = next((record for record in records if category in record["categories"]), None)
        if match:
            edge_records.append(match)
    for record in edge_records:
        add(record)

    for record in sorted(records, key=lambda value: (record_hash(source_hash, value["event_id"]), value["event_id"])):
        add(record)
    return list(selected.values()), f"pilot of {count}: human keeps, EventKit edge cases, then deterministic hash sample"


def archive_notes(record: dict[str, Any]) -> str:
    """Match normal imports without exposing archive/classification metadata."""
    blocks = [record["notes"]] if record["notes"] else []
    context = []
    for label, field in (("Project", "project"), ("Heading", "heading")):
        if record.get(field):
            context.append(f"{label}: {planner.sanitize_component(record[field], label)}")
    tags = record.get("tags") or []
    if tags:
        context.append(
            "Tags: " + " ".join(
                f"#{planner.sanitize_component(tag, 'tag')}" for tag in tags
            )
        )
    checklist_items = record.get("checklist_items")
    if checklist_items is not None:
        if checklist_items:
            context.append(
                "Checklist:\n" + "\n".join(
                    f"- [{'x' if item['status'] == 'completed' else ' '}] "
                    f"{planner.sanitize_component(item['title'], 'Untitled checklist item')}"
                    for item in checklist_items
                )
            )
    elif record["checklist"]:
        context.append("Checklist:\n" + record["checklist"])
    if context:
        blocks.append("\n".join(context))
    return "\n\n".join(blocks)


def reminder_title(record: dict[str, Any]) -> tuple[str, str | None]:
    if record["title"]:
        return record["title"], None
    generated = f"Untitled Things archive item [{record['event_id'][:8]}]"
    return generated, "Reminders requires a visible title; the original empty title remains preserved in sourceTitle and the immutable source index"


def build_archive_plan(
    document: dict[str, Any],
    run_id: str,
    list_prefix: str,
    count: int | None = None,
) -> tuple[dict[str, Any], str]:
    selected, selection_policy = select_records(document, count)
    base_list_title = f"{list_prefix} — {run_id}"
    identities_by_leaf: dict[str, set[str]] = {}
    for record in document["records"]:
        area = record.get("area")
        leaf = planner.sanitize_component(str(area or "No Area"), "No Area")
        identity = str(record.get("area_id") or ("__inbox__" if not area else f"legacy:{leaf.casefold()}"))
        identities_by_leaf.setdefault(leaf.casefold(), set()).add(identity)

    def list_title_for(record: dict[str, Any]) -> str:
        area = record.get("area")
        leaf = planner.sanitize_component(str(area or "No Area"), "No Area")
        identity = str(record.get("area_id") or ("__inbox__" if not area else f"legacy:{leaf.casefold()}"))
        if len(identities_by_leaf[leaf.casefold()]) > 1:
            suffix = "no-area" if identity == "__inbox__" else planner.sanitize_component(identity, "area")[:8]
            leaf = f"{leaf} [{suffix}]"
        return f"{base_list_title} — {leaf}"

    items: list[dict[str, Any]] = []
    empty_title_ids: list[str] = []
    for record in selected:
        title, warning = reminder_title(record)
        if warning:
            empty_title_ids.append(record["event_id"])
        completion_date = normalize_timestamp(record["date"], record["event_id"])
        items.append({
            "sourceID": record["event_id"],
            "sourceInstanceID": record["event_id"],
            "sourceTitle": record["title"],
            "title": title,
            "notes": archive_notes(record),
            "url": record["things_url"],
            "listTitle": list_title_for(record),
            "dueDate": local_archive_date(record["date"], record["event_id"]),
            "alarmDate": None,
            "priority": 0,
            "recurrenceRules": [],
            "recurrenceOriginalMode": None,
            "completed": True,
            "completionDate": completion_date,
            "warnings": [warning] if warning else [],
            "archiveCategories": record["categories"],
            "archiveTier": record["tier"],
            "sourceStatus": record["status"],
            "sourceAreaID": record.get("area_id"),
            "sourceArea": record.get("area"),
        })
    items.sort(key=lambda item: (item["completionDate"], item["sourceID"]))
    calendar_titles = sorted({item["listTitle"] for item in items}, key=str.casefold)
    status_counts = Counter(item["sourceStatus"] for item in items)
    tier_counts = Counter(item["archiveTier"] for item in items)
    category_counts = Counter(category for item in items for category in item["archiveCategories"])
    warnings = []
    if status_counts.get("canceled"):
        warnings.append(
            f"{status_counts['canceled']} canceled Things records are represented as completed Reminders so the archive stays hidden; their original status remains in the private plan"
        )
    if empty_title_ids:
        warnings.append(
            f"{len(empty_title_ids)} records have empty source titles and use generated Reminders labels; the empty originals remain in the private plan and source index"
        )
    plan = {
        "schema": PLAN_SCHEMA,
        "planKind": "verbatim_archive",
        "runID": run_id,
        "createdAt": planner.now_utc(),
        "databaseVersion": None,
        "afterCompletionPolicy": "not_applicable",
        "unsupportedPolicy": "abort",
        "recurringProjectPolicy": "not_applicable",
        "listPrefix": base_list_title,
        "calendarTitles": calendar_titles,
        "items": items,
        "unsupported": [],
        "warnings": warnings,
        "blocked": False,
        "archiveSource": {
            "schema": document["schema"],
            "sourceSHA256": document["source_sha256"],
            "selectionPolicy": selection_policy,
            "sourceRecords": len(document["records"]),
            "selectedRecords": len(items),
        },
    }
    report = "\n".join([
        f"Run ID: {run_id}",
        "Plan kind: verbatim Things knowledge archive",
        f"Source snapshot SHA-256: {document['source_sha256']}",
        f"Source index records: {len(document['records'])}",
        f"Planned completed reminders: {len(items)}",
        f"Destination lists by Things Area: {len(calendar_titles)}",
        f"Selection: {selection_policy}",
        "Source statuses: " + ", ".join(f"{key}={value}" for key, value in sorted(status_counts.items())),
        "Tiers: " + ", ".join(f"{key}={value}" for key, value in sorted(tier_counts.items())),
        "Categories: " + ", ".join(f"{key}={value}" for key, value in sorted(category_counts.items())),
        f"Longest title: {max(map(lambda item: len(item['sourceTitle']), items), default=0)} characters",
        f"Longest generated notes body: {max(map(lambda item: len(item['notes']), items), default=0)} characters",
        f"Empty source titles adapted: {len(empty_title_ids)}",
        "",
        *(["WARNINGS", *(f"- {warning}" for warning in warnings), ""] if warnings else []),
        "No Apple Reminders data has been changed. Review plan.json and plan.csv before apply.",
        "",
    ])
    return plan, report


def write_archive_review_files(run_dir: Path, plan: dict[str, Any]) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=".plan.csv.", dir=run_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output)
            writer.writerow([
                "source_id", "source_title", "reminders_title", "source_status", "categories",
                "tier", "completion_date", "list", "warnings",
            ])
            for item in plan["items"]:
                writer.writerow([
                    item["sourceID"], item["sourceTitle"], item["title"], item["sourceStatus"],
                    " ".join(item["archiveCategories"]), item["archiveTier"], item["completionDate"],
                    item["listTitle"], " | ".join(item["warnings"]),
                ])
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp_name, run_dir / "plan.csv")
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise

    rollback = f"""MANUAL ROLLBACK — ARCHIVE RUN {plan['runID']}

Preferred after apply:
  ./things-reminders rollback "{run_dir}"

Without the tool:
1. Keep this run directory and use ROLLBACK_INVENTORY.txt for exact EventKit IDs.
2. Delete only reminders created by this run.
3. Delete an isolated archive list only if it contains no reminders you added manually.

The immutable source index and plan.json remain the authoritative reconstruction source.
"""
    planner.atomic_write(run_dir / "MANUAL_ROLLBACK.txt", rollback.encode())


def default_run_dir(run_id: str) -> Path:
    return Path.home() / "Documents" / "Things Reminders Migration" / run_id


def cmd_archive_plan(args: argparse.Namespace) -> int:
    if args.count is not None and args.count < 1:
        raise planner.PlanError("--count must be positive")
    index_path = Path(args.index).expanduser().resolve()
    document, raw = read_index(index_path)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = args.run_id or f"archive-{stamp}-{secrets.token_hex(3)}"
    run_dir = Path(args.run_dir).expanduser().resolve() if args.run_dir else default_run_dir(run_id)
    if run_dir.exists() and any(run_dir.iterdir()):
        raise planner.PlanError(f"Run directory is not empty: {run_dir}")
    run_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(run_dir, 0o700)

    source_copy = run_dir / "source-index.json"
    planner.atomic_write(source_copy, raw)
    source_info = {
        "originalPath": str(index_path),
        "copiedPath": str(source_copy),
        "indexSHA256": hashlib.sha256(raw).hexdigest(),
        "sourceSnapshotSHA256": document["source_sha256"],
        "copiedAt": planner.now_utc(),
    }
    planner.atomic_json(run_dir / "source.json", source_info)
    plan, report = build_archive_plan(document, run_id, args.list_prefix, args.count)
    planner.atomic_json(run_dir / "plan.json", plan)
    planner.atomic_write(run_dir / "report.txt", report.encode())
    write_archive_review_files(run_dir, plan)
    planner.atomic_json(run_dir / "state.json", {
        "schema": "things-reminders-state/v1",
        "runID": run_id,
        "status": "planned",
        "updatedAt": planner.now_utc(),
    })
    print(report, end="")
    print(f"Run directory: {run_dir}")
    return 0


def parser_for_archive() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--index", required=True, help="things-verbatim-knowledge-index/v1 JSON")
    value.add_argument("--run-dir")
    value.add_argument("--run-id")
    value.add_argument("--list-prefix", default="Things Knowledge Archive")
    value.add_argument("--count", type=int, help="create a deterministic pilot plan with at most this many records")
    value.set_defaults(func=cmd_archive_plan)
    return value


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser_for_archive().parse_args(argv)
        return int(args.func(args))
    except planner.PlanError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; no Reminders changes were made by the archive planner.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
