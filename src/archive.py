#!/usr/bin/env python3
"""Create a verbatim Things archive and calibrate decisions, insights, and facts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import archive_support as support

SCHEMA = "things-verbatim-archive/v1"
LABEL_SCHEMA = "things-verbatim-record-labels/v1"

RARE_EVENT_RE = re.compile(
    r"\b(?:спермограмм\w*|анализ\w*|мрт|кт|узи|рентген\w*|вакцин\w*|привив\w*|"
    r"операци\w*|обследован\w*|при[её]м у врач\w*|дерматолог\w*|уролог\w*|стоматолог\w*|"
    r"родил\w*|родился|беремен\w*|переехал\w*|переезд\w*|виз\w*|паспорт\w*|"
    r"купил\w* (?:квартир\w*|машин\w*|автомобил\w*)|продал\w* (?:квартир\w*|машин\w*)|"
    r"уволил\w*|увольнен\w*|повысил\w*|повышен\w*|собеседован\w*|ассесмент\w*|"
    r"выступил\w*|доклад\w*|воркшоп\w*|опубликовал\w*|выпустил\w*|окончил\w*|закончил\w* курс\w*|"
    r"spermogram|blood test|mri|ultrasound|vaccin\w*|surgery|medical exam|moved|relocat\w*|"
    r"visa|passport|bought (?:a )?(?:home|house|apartment|car)|sold (?:a )?(?:home|house|apartment|car)|"
    r"promot\w*|resign\w*|interview\w*|assessment\w*|gave a talk|presented|published)\b",
    re.I,
)
DATED_EVENT_RE = re.compile(
    r"^(?:сделать (?:спермограмм\w*|мрт|кт|узи|рентген\w*)|"
    r"сдать (?:спермограмм\w*|анализ\w*|кровь)|пройти (?:обследован\w*|медосмотр\w*|"
    r"собеседован\w*|ассесмент\w*|курс\w*|вакцинаци\w*)|"
    r"(?:сходить|пойти) (?:к (?:врач\w*|дерматолог\w*|уролог\w*|стоматолог\w*)|на при[её]м)|"
    r"(?:поставить|сделать) прививк\w*|(?:провести|сделать) операци\w*|"
    r"переехат\w*|прилетет\w*|улетет\w*|выступит\w*|опубликоват\w*|выпустит\w*|"
    r"купить (?:квартир\w*|дом\w*|машин\w*|автомобил\w*)|"
    r"have (?:a )?(?:spermogram|blood test|mri|ultrasound|surgery)|take (?:a )?(?:blood test|medical exam)|"
    r"get vaccin\w*|move|relocat\w*|give a talk|present|publish|release|"
    r"buy (?:a )?(?:home|house|apartment|car))\b",
    re.I,
)
MEDICAL_DATED_RE = re.compile(
    r"^(?:сделать (?:спермограмм\w*|мрт|кт|узи|рентген\w*)|"
    r"сдать (?:спермограмм\w*|анализ\w*|кровь)|пройти (?:обследован\w*|медосмотр\w*|вакцинаци\w*)|"
    r"(?:сходить|пойти) (?:к (?:врач\w*|дерматолог\w*|уролог\w*|стоматолог\w*)|на при[её]м)|"
    r"(?:поставить|сделать) прививк\w*|(?:провести|сделать) операци\w*|"
    r"have (?:a )?(?:spermogram|blood test|mri|ultrasound|surgery)|"
    r"take (?:a )?(?:blood test|medical exam)|get vaccin\w*)\b",
    re.I,
)
ROUTINE_RE = re.compile(
    r"^(?:купить (?:еду|продукт\w*|молоко|кофе)|убраться|помыть|почистить|постирать|"
    r"тренировк\w*|зарядк\w*|прогулк\w*|почитать|посмотреть почт\w*|разобрать инбокс|"
    r"stand.?up|daily|weekly|clean|wash|grocer\w*|workout|exercise|walk)\b",
    re.I,
)
FACT_MARKER_RE = re.compile(
    r"(?:\b\d{1,4}(?:[.,]\d+)?\s*(?:₽|руб\w*|%|мм|см|кг|г|мл|л|мг|mg|gb|mb|дн\w*|час\w*)\b|"
    r"\b(?:был[аио]?|стало|получил\w*|купил\w*|сдал\w*|прош[её]л\w*|назначил\w*|"
    r"результат\w*|диагноз\w*|стоил\w*|дата|итого|result|measured|cost|diagnos\w*|completed)\b)",
    re.I,
)


class ArchiveError(RuntimeError):
    pass


def private_write(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def deterministic(value: str, seed: str = "verbatim-calibration-v1") -> bytes:
    return hashlib.sha256(f"{seed}\0{value}".encode()).digest()


def create_archive(snapshot: Path, output: Path) -> Path:
    if output.exists() and any(output.iterdir()):
        raise ArchiveError(f"Output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)
    source = support.open_ro(snapshot)
    try:
        support.require_schema(source)
        events = support.fetch_source_events(source)
    finally:
        source.close()
    global_titles = Counter(support.normalize_title(value["title"]) for value in events)
    area_titles = Counter(
        (value["area"]["id"] or "__inbox__", support.normalize_title(value["title"]))
        for value in events
    )
    repeat_sizes = Counter(
        value["repeat"]["template_id"] for value in events if value["repeat"]["template_id"]
    )
    archive = output / "archive.sqlite"
    connection = sqlite3.connect(archive)
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE event(
              id TEXT PRIMARY KEY,
              role TEXT NOT NULL,
              kind TEXT NOT NULL,
              status TEXT NOT NULL,
              title TEXT NOT NULL,
              notes TEXT NOT NULL,
              checklist_text TEXT NOT NULL,
              created_at TEXT,
              modified_at TEXT,
              stopped_at TEXT,
              area_id TEXT,
              area_title TEXT,
              project_id TEXT,
              project_title TEXT,
              heading_id TEXT,
              heading_title TEXT,
              repeat_template_id TEXT,
              repeat_template_title TEXT,
              tags_json TEXT NOT NULL,
              normalized_title TEXT NOT NULL,
              global_title_count INTEGER NOT NULL,
              area_title_count INTEGER NOT NULL,
              repeat_series_size INTEGER NOT NULL,
              notes_hash TEXT,
              content_hash TEXT NOT NULL,
              possible_sensitive INTEGER NOT NULL,
              rare_event_hint INTEGER NOT NULL,
              dated_event_hint INTEGER NOT NULL,
              routine_hint INTEGER NOT NULL,
              payload_json TEXT NOT NULL
            );
            CREATE INDEX event_notes_hash ON event(notes_hash);
            CREATE INDEX event_normalized_title ON event(normalized_title);
            CREATE INDEX event_stopped_at ON event(stopped_at);
            CREATE VIRTUAL TABLE event_fts USING fts5(
              event_id UNINDEXED,title,notes,checklist,context,
              tokenize='unicode61 remove_diacritics 2'
            );
            """
        )
        metadata = {
            "schema": SCHEMA,
            "source_path": str(snapshot),
            "source_sha256": support.file_sha256(snapshot),
            "created_at": support.now_utc(),
        }
        connection.executemany("INSERT INTO meta VALUES(?,?)", metadata.items())
        for event in events:
            normalized = support.normalize_title(event["title"])
            area_key = event["area"]["id"] or "__inbox__"
            notes = event["notes"]
            checklist_text = "\n".join(
                f"[{item['status']}] {item['title']}" for item in event["checklist"]
            )
            searchable = "\n".join([event["title"], notes, checklist_text])
            context = " / ".join(
                value for value in [
                    event["area"]["title"],
                    event["project"]["title"],
                    event["heading"]["title"],
                ] if value
            )
            repeat_id = event["repeat"]["template_id"]
            rare = bool(RARE_EVENT_RE.search(event["title"]))
            dated_event = bool(DATED_EVENT_RE.search(event["title"]))
            routine = bool(ROUTINE_RE.search(event["title"])) or bool(repeat_id)
            row = (
                event["id"], event["role"], event["kind"], event["status"], event["title"],
                notes, checklist_text, event["created_at"], event["modified_at"], event["stopped_at"],
                event["area"]["id"], event["area"]["title"], event["project"]["id"],
                event["project"]["title"], event["heading"]["id"], event["heading"]["title"],
                repeat_id, event["repeat"]["template_title"],
                json.dumps(event["tags"], ensure_ascii=False), normalized,
                global_titles[normalized], area_titles[(area_key, normalized)],
                repeat_sizes[repeat_id] if repeat_id else 0,
                support.sha256_text(notes) if notes else None,
                support.event_content_hash(event),
                int(bool(support.SENSITIVE_RE.search(searchable))), int(rare), int(dated_event), int(routine),
                json.dumps(event, ensure_ascii=False, sort_keys=True),
            )
            connection.execute("INSERT INTO event VALUES(" + ",".join("?" for _ in row) + ")", row)
            connection.execute(
                "INSERT INTO event_fts VALUES(?,?,?,?,?)",
                (event["id"], event["title"], notes, checklist_text, context),
            )
        connection.commit()
        checks = {
            "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "events": connection.execute("SELECT count(*) FROM event").fetchone()[0],
            "unique_ids": connection.execute("SELECT count(DISTINCT id) FROM event").fetchone()[0],
            "fts_rows": connection.execute("SELECT count(*) FROM event_fts").fetchone()[0],
        }
        if checks["integrity_check"] != "ok" or checks["events"] != len(events) or checks["unique_ids"] != len(events) or checks["fts_rows"] != len(events):
            raise ArchiveError(f"Archive validation failed: {checks}")
    finally:
        connection.close()
    os.chmod(archive, 0o600)
    profile = archive_profile(archive)
    private_write(output / "profile.json", json.dumps(profile, ensure_ascii=False, indent=2) + "\n")
    return archive


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def archive_profile(archive: Path) -> dict[str, Any]:
    connection = connect(archive)
    try:
        result: dict[str, Any] = {
            "schema": SCHEMA,
            "events": connection.execute("SELECT count(*) FROM event").fetchone()[0],
            "completed": connection.execute("SELECT count(*) FROM event WHERE status='completed'").fetchone()[0],
            "canceled": connection.execute("SELECT count(*) FROM event WHERE status='canceled'").fetchone()[0],
            "archived_unfinished_children": connection.execute("SELECT count(*) FROM event WHERE role='archived_unfinished_child'").fetchone()[0],
            "with_notes": connection.execute("SELECT count(*) FROM event WHERE notes!=''").fetchone()[0],
            "unique_note_fragments": connection.execute("SELECT count(DISTINCT notes_hash) FROM event WHERE notes_hash IS NOT NULL").fetchone()[0],
            "title_only_completed": connection.execute("SELECT count(*) FROM event WHERE status='completed' AND notes='' AND checklist_text=''").fetchone()[0],
            "repeating_events": connection.execute("SELECT count(*) FROM event WHERE repeat_template_id IS NOT NULL").fetchone()[0],
            "sensitive_quarantine": connection.execute("SELECT count(*) FROM event WHERE possible_sensitive=1").fetchone()[0],
            "rare_event_hints": connection.execute("SELECT count(*) FROM event WHERE rare_event_hint=1 AND status='completed'").fetchone()[0],
            "dated_event_hints": connection.execute("SELECT count(*) FROM event WHERE dated_event_hint=1 AND status='completed'").fetchone()[0],
        }
        return result
    finally:
        connection.close()


def record(row: sqlite3.Row, channel: str) -> dict[str, Any]:
    contexts = [value for value in [row["area_title"], row["project_title"], row["heading_title"]] if value]
    return {
        "event_id": str(row["id"]),
        "channel": channel,
        "title": str(row["title"]),
        "notes": str(row["notes"]),
        "checklist_text": str(row["checklist_text"]),
        "status": str(row["status"]),
        "role": str(row["role"]),
        "kind": str(row["kind"]),
        "date": row["stopped_at"] or row["modified_at"] or row["created_at"],
        "contexts": contexts,
        "global_title_count": int(row["global_title_count"]),
        "area_title_count": int(row["area_title_count"]),
        "repeat_series_size": int(row["repeat_series_size"]),
        "repeat_template_title": row["repeat_template_title"],
        "things_url": f"things:///show?id={row['id']}",
    }


def select_review(archive: Path, total: int) -> list[dict[str, Any]]:
    connection = connect(archive)
    try:
        rows = connection.execute("SELECT * FROM event WHERE possible_sensitive=0").fetchall()
    finally:
        connection.close()
    channels: list[tuple[str, list[sqlite3.Row]]] = [
        (
            "decision_in_text",
            [row for row in rows if row["notes"] and support.DECISION_MARKER_RE.search(f"{row['title']}\n{row['notes']}")],
        ),
        (
            "insight_in_text",
            [row for row in rows if row["notes"] and support.INSIGHT_MARKER_RE.search(f"{row['title']}\n{row['notes']}")],
        ),
        (
            "registered_fact",
            [row for row in rows if row["notes"] and (FACT_MARKER_RE.search(row["notes"]) or support.SPEC_RE.search(row["notes"]))],
        ),
        (
            "dated_event_candidate",
            [row for row in rows if row["status"] == "completed" and row["dated_event_hint"] and not row["repeat_template_id"]],
        ),
        (
            "rare_event_boundary",
            [row for row in rows if row["status"] == "completed" and row["rare_event_hint"] and not row["dated_event_hint"] and not row["repeat_template_id"]],
        ),
        (
            "routine_control",
            [row for row in rows if row["status"] == "completed" and not row["notes"] and (row["routine_hint"] or row["global_title_count"] >= 5)],
        ),
    ]
    selected: list[dict[str, Any]] = []
    used_events: set[str] = set()
    used_note_hashes: set[str] = set()
    title_counts: Counter[str] = Counter()
    special_counts: Counter[str] = Counter()

    def add(row: sqlite3.Row, channel: str) -> bool:
        event_id = str(row["id"])
        note_hash = str(row["notes_hash"] or "")
        normalized = str(row["normalized_title"])
        special = "spermogram" if "спермограмм" in str(row["title"]).casefold() else ""
        if (
            event_id in used_events
            or (note_hash and note_hash in used_note_hashes)
            or title_counts[normalized] >= 2
            or (special and special_counts[special] >= 1)
        ):
            return False
        selected.append(record(row, channel))
        used_events.add(event_id)
        if note_hash:
            used_note_hashes.add(note_hash)
        title_counts[normalized] += 1
        if special:
            special_counts[special] += 1
        return True

    quota = max(1, total // len(channels))
    for channel, candidates in channels:
        if channel in {"decision_in_text", "insight_in_text", "registered_fact"}:
            candidates.sort(
                key=lambda row: (
                    not (80 <= len(str(row["notes"])) <= 5000),
                    deterministic(str(row["id"])),
                )
            )
        elif channel in {"dated_event_candidate", "rare_event_boundary"}:
            candidates.sort(
                key=lambda row: (
                    0 if channel == "dated_event_candidate" and MEDICAL_DATED_RE.search(str(row["title"])) else 1,
                    "спермограмм" not in str(row["title"]).casefold(),
                    bool(row["notes"] or row["checklist_text"]),
                    row["global_title_count"],
                    deterministic(str(row["id"])),
                )
            )
        else:
            candidates.sort(key=lambda row: deterministic(str(row["id"])))
        added = 0
        for row in candidates:
            if add(row, channel):
                added += 1
            if added >= quota:
                break
    if len(selected) < total:
        for row in sorted(rows, key=lambda value: deterministic(str(value["id"]))):
            channel = "content_fallback" if row["notes"] or row["checklist_text"] else "event_fallback"
            add(row, channel)
            if len(selected) >= total:
                break
    return selected[:total]


def import_labels(archive: Path, labels_path: Path) -> dict[str, Any]:
    document = json.loads(labels_path.read_text(encoding="utf-8"))
    if document.get("schema") != LABEL_SCHEMA:
        raise ArchiveError("Unsupported verbatim-label schema")
    labels = document.get("labels")
    if not isinstance(labels, list):
        raise ArchiveError("Label document has no labels array")
    allowed_categories = {"decision", "insight", "registered_fact", "dated_event_fact"}
    allowed_dispositions = {None, "drop", "sensitive"}
    connection = connect(archive)
    try:
        source_hash = connection.execute("SELECT value FROM meta WHERE key='source_sha256'").fetchone()[0]
        if document.get("source_sha256") != source_hash:
            raise ArchiveError("Labels belong to another Things snapshot")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS human_label(
              event_id TEXT PRIMARY KEY REFERENCES event(id),
              categories_json TEXT NOT NULL,
              disposition TEXT,
              comment TEXT NOT NULL,
              interpretation TEXT NOT NULL,
              labeled_at TEXT NOT NULL,
              source_file TEXT NOT NULL,
              raw_json TEXT NOT NULL
            )
            """
        )
        seen: set[str] = set()
        counts: Counter[str] = Counter()
        overrides = 0
        rows = []
        for value in labels:
            if not isinstance(value, dict):
                raise ArchiveError("Every label must be an object")
            event_id = str(value.get("event_id", ""))
            categories = value.get("categories", [])
            disposition = value.get("disposition")
            comment = str(value.get("comment", ""))
            if event_id in seen or not connection.execute("SELECT 1 FROM event WHERE id=?", (event_id,)).fetchone():
                raise ArchiveError(f"Unknown or duplicate label event: {event_id}")
            if not isinstance(categories, list) or not categories or any(value not in allowed_categories for value in categories):
                if categories != []:
                    raise ArchiveError(f"Invalid categories for {event_id}")
            if disposition not in allowed_dispositions or (categories and disposition):
                raise ArchiveError(f"Invalid disposition for {event_id}")
            if not categories and not disposition:
                raise ArchiveError(f"Label has no decision for {event_id}")
            if comment.strip().casefold() == "drop" and categories:
                categories, disposition = [], "drop"
                overrides += 1
            interpretation = disposition or "keep"
            counts[interpretation] += 1
            for category in set(categories):
                counts[f"category:{category}"] += 1
            rows.append(
                (
                    event_id,
                    json.dumps(sorted(set(categories)), ensure_ascii=False),
                    disposition,
                    comment,
                    interpretation,
                    support.now_utc(),
                    str(labels_path),
                    json.dumps(value, ensure_ascii=False, sort_keys=True),
                )
            )
            seen.add(event_id)
        connection.executemany("INSERT OR REPLACE INTO human_label VALUES(?,?,?,?,?,?,?,?)", rows)
        connection.commit()
        return {
            "labels": len(rows),
            "counts": dict(sorted(counts.items())),
            "comment_drop_overrides": overrides,
        }
    finally:
        connection.close()


def label_report(archive: Path, sample_path: Path) -> dict[str, Any]:
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    channels = {value["event_id"]: value["channel"] for value in sample["records"]}
    connection = connect(archive)
    try:
        rows = connection.execute("SELECT * FROM human_label ORDER BY event_id").fetchall()
        by_channel: dict[str, Counter[str]] = {}
        categories: Counter[str] = Counter()
        dispositions: Counter[str] = Counter()
        for row in rows:
            channel = channels.get(str(row["event_id"]), "outside_sample")
            by_channel.setdefault(channel, Counter())[str(row["interpretation"])] += 1
            dispositions[str(row["interpretation"])] += 1
            categories.update(json.loads(row["categories_json"]))
        return {
            "labels": len(rows),
            "dispositions": dict(sorted(dispositions.items())),
            "categories": dict(sorted(categories.items())),
            "channels": {key: dict(sorted(value.items())) for key, value in sorted(by_channel.items())},
        }
    finally:
        connection.close()


def validate_review(records: list[dict[str, Any]], archive: Path) -> None:
    connection = connect(archive)
    try:
        seen: set[str] = set()
        for value in records:
            event_id = str(value["event_id"])
            if event_id in seen:
                raise ArchiveError(f"Duplicate review event: {event_id}")
            row = connection.execute("SELECT * FROM event WHERE id=?", (event_id,)).fetchone()
            if not row or row["possible_sensitive"]:
                raise ArchiveError(f"Unknown or sensitive review event: {event_id}")
            if value["title"] != row["title"] or value["notes"] != row["notes"] or value["checklist_text"] != row["checklist_text"]:
                raise ArchiveError(f"Review text is not verbatim: {event_id}")
            seen.add(event_id)
    finally:
        connection.close()


def write_review(archive: Path, sample_path: Path, html_path: Path, total: int) -> dict[str, Any]:
    records = select_review(archive, total)
    validate_review(records, archive)
    connection = connect(archive)
    try:
        source_hash = connection.execute("SELECT value FROM meta WHERE key='source_sha256'").fetchone()[0]
    finally:
        connection.close()
    sample = {
        "schema": "things-verbatim-calibration/v1",
        "source_sha256": source_hash,
        "created_at": support.now_utc(),
        "records": records,
    }
    private_write(sample_path, json.dumps(sample, ensure_ascii=False, indent=2) + "\n")
    embedded = json.dumps(sample, ensure_ascii=False).replace("</", "<\\/")
    document = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="referrer" content="no-referrer"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Things verbatim knowledge calibration</title><style>
:root{--bg:#f2f0eb;--card:#fff;--ink:#24221f;--muted:#6f6a62;--line:#d8d2c8;--accent:#2f63bb}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}header{position:sticky;top:0;background:#f2f0ebf2;border-bottom:1px solid var(--line);z-index:2}.bar{max-width:1000px;margin:auto;padding:12px 20px;display:flex;justify-content:space-between;align-items:center}main{max-width:1000px;margin:auto;padding:22px 20px 80px}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px}.meta{color:var(--muted);font-size:13px}.source{white-space:pre-wrap;overflow-wrap:anywhere;background:#f8f6f2;padding:14px;border-radius:8px;font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;max-height:520px;overflow:auto}.actions{display:flex;flex-wrap:wrap;gap:7px;margin:16px 0}.actions button,.nav,.download{padding:8px 11px;border:1px solid var(--line);background:white;border-radius:8px}.actions button.selected{outline:3px solid var(--accent);background:#e8effc}.actions button.terminal{border-color:#b96b6b}textarea{width:100%;min-height:70px;padding:9px}.channel{display:inline-block;background:#e9e5dc;border-radius:999px;padding:3px 8px;font-size:12px}
</style></head><body><header><div class="bar"><button class="nav" id="prev">← Previous</button><strong id="progress"></strong><button class="nav" id="next">Next →</button></div></header><main><div id="app"></div><button class="download" id="download">Download exact-record labels JSON</button><p class="meta">Private local page. Exact Things source only; nothing is uploaded.</p></main><script id="payload" type="application/json">__DATA__</script><script>
const data=JSON.parse(document.getElementById('payload').textContent),key='things-verbatim-labels:'+data.source_sha256;let state=JSON.parse(localStorage.getItem(key)||'{}'),index=0;const categories=[['decision','Decision'],['insight','Insight'],['registered_fact','Fact in title/notes'],['dated_event_fact','Rare dated event']];function node(tag,text,cls){const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e}function save(){localStorage.setItem(key,JSON.stringify(state))}function current(r){return state[r.event_id]||{categories:[]}}function done(r){const s=current(r);return s.disposition||s.categories?.length}function progress(){document.getElementById('progress').textContent=`${index+1}/${data.records.length} · ${data.records.filter(done).length} labeled`}function render(){const r=data.records[index],s=current(r),app=document.getElementById('app');app.replaceChildren();const card=node('article',undefined,'card');card.append(node('span',r.channel,'channel'));card.append(node('h1',r.title));card.append(node('p',`${r.status} · ${r.date||'no date'} · ${r.contexts.join(' / ')||'No context'}`,'meta'));card.append(node('p',`Same title: ${r.global_title_count} globally / ${r.area_title_count} in area · repeat series: ${r.repeat_series_size||0}`,'meta'));const source=[r.notes,r.checklist_text].filter(Boolean).join('\\n\\nChecklist:\\n');card.append(node('pre',source||'(No notes: the completed title and date are the entire candidate fact.)','source'));const controls=node('div',undefined,'actions');for(const [value,label] of categories){const b=node('button',label);if((s.categories||[]).includes(value))b.className='selected';b.onclick=()=>{const values=new Set(current(r).categories||[]);values.has(value)?values.delete(value):values.add(value);state[r.event_id]={...current(r),categories:[...values],disposition:values.size?null:current(r).disposition};save();render()};controls.append(b)}for(const [value,label] of [['drop','Drop'],['sensitive','Sensitive']]){const b=node('button',label,'terminal');if(s.disposition===value)b.className='selected terminal';b.onclick=()=>{state[r.event_id]={...current(r),categories:[],disposition:value};save();render()};controls.append(b)}card.append(controls);const comment=node('textarea');comment.placeholder='Optional boundary comment';comment.value=s.comment||'';comment.oninput=()=>{state[r.event_id]={...current(r),comment:comment.value};save();progress()};card.append(comment);const link=node('a','Open original in Things');link.href=r.things_url;card.append(link);app.append(card);progress()}document.getElementById('prev').onclick=()=>{index=(index-1+data.records.length)%data.records.length;render()};document.getElementById('next').onclick=()=>{index=(index+1)%data.records.length;render()};document.addEventListener('keydown',e=>{if(e.target.tagName==='TEXTAREA')return;if(e.key==='ArrowLeft')document.getElementById('prev').click();if(e.key==='ArrowRight')document.getElementById('next').click();const n=Number(e.key);if(n>=1&&n<=4)document.querySelectorAll('.actions button')[n-1].click();if(e.key==='5')document.querySelectorAll('.actions button')[4].click();if(e.key==='6')document.querySelectorAll('.actions button')[5].click()});document.getElementById('download').onclick=()=>{const output={schema:'things-verbatim-record-labels/v1',source_sha256:data.source_sha256,labels:data.records.map(r=>({event_id:r.event_id,...current(r)})).filter(x=>x.disposition||x.categories?.length)};const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(output,null,2)],{type:'application/json'}));a.download='things-verbatim-record-labels.json';a.click();URL.revokeObjectURL(a.href)};render();
</script></body></html>""".replace("__DATA__", embedded)
    private_write(html_path, document)
    return {
        "records": len(records),
        "channels": dict(sorted(Counter(value["channel"] for value in records).items())),
        "sample_sha256": support.file_sha256(sample_path),
        "review_sha256": support.file_sha256(html_path),
    }


def heuristic_categories(row: sqlite3.Row) -> tuple[list[str], dict[str, int]]:
    text = "\n".join([str(row["title"]), str(row["notes"]), str(row["checklist_text"])])
    decision_hits = len(support.DECISION_MARKER_RE.findall(text))
    insight_hits = len(support.INSIGHT_MARKER_RE.findall(text))
    fact_hits = len(FACT_MARKER_RE.findall(text)) + len(support.SPEC_RE.findall(text))
    dated_hits = int(
        row["status"] == "completed" and bool(row["rare_event_hint"] or row["dated_event_hint"])
    )
    categories = []
    if decision_hits:
        categories.append("decision")
    if insight_hits:
        categories.append("insight")
    if fact_hits:
        categories.append("registered_fact")
    if dated_hits:
        categories.append("dated_event_fact")
    return categories, {
        "decision": decision_hits,
        "insight": insight_hits,
        "registered_fact": fact_hits,
        "dated_event_fact": dated_hits,
    }


def build_knowledge_index(archive: Path, json_path: Path, html_path: Path) -> dict[str, Any]:
    if json_path.exists() or html_path.exists():
        raise ArchiveError("Knowledge-index output already exists")
    connection = connect(archive)
    try:
        labels = {
            str(row["event_id"]): row
            for row in connection.execute("SELECT * FROM human_label")
        }
        source_hash = connection.execute("SELECT value FROM meta WHERE key='source_sha256'").fetchone()[0]
        records = []
        heuristic_labeled_predictions: set[str] = set()
        labeled_keep = labeled_drop = labeled_sensitive = 0
        for row in connection.execute("SELECT * FROM event ORDER BY id"):
            event_id = str(row["id"])
            label = labels.get(event_id)
            categories, signals = heuristic_categories(row)
            if label and label["interpretation"] != "sensitive" and categories:
                heuristic_labeled_predictions.add(event_id)
            if label and label["interpretation"] == "drop":
                labeled_drop += 1
                continue
            if label and label["interpretation"] == "sensitive":
                labeled_sensitive += 1
                continue
            if row["possible_sensitive"] or row["project_title"] == "Ежемесячная финансовая рутина":
                continue
            tier = "broad"
            if label and label["interpretation"] == "keep":
                categories = json.loads(label["categories_json"])
                tier = "human_keep"
                labeled_keep += 1
            elif not categories:
                continue
            elif len(categories) >= 2 or sum(signals.values()) >= 3:
                tier = "strong"
            score = (
                (1000 if tier == "human_keep" else 100 if tier == "strong" else 0)
                + min(20, signals["decision"] * 3)
                + min(20, signals["insight"] * 3)
                + min(20, signals["registered_fact"] * 2)
                + signals["dated_event_fact"] * 5
                + min(5, len(str(row["notes"])) // 500)
            )
            payload = json.loads(row["payload_json"])
            records.append(
                {
                    "event_id": event_id,
                    "categories": sorted(categories),
                    "tier": tier,
                    "score": score,
                    "signals": signals,
                    "title": str(row["title"]),
                    "notes": str(row["notes"]),
                    "checklist": str(row["checklist_text"]),
                    "status": str(row["status"]),
                    "date": row["stopped_at"] or row["modified_at"] or row["created_at"],
                    "context": [
                        value for value in [row["area_title"], row["project_title"], row["heading_title"]]
                        if value
                    ],
                    "area_id": row["area_id"],
                    "area": row["area_title"],
                    "project": row["project_title"],
                    "heading": row["heading_title"],
                    "tags": json.loads(row["tags_json"]),
                    "checklist_items": payload["checklist"],
                    "global_title_count": int(row["global_title_count"]),
                    "repeat_series_size": int(row["repeat_series_size"]),
                    "things_url": f"things:///show?id={event_id}",
                }
            )
        records.sort(key=lambda value: (-value["score"], value["date"] or "", value["event_id"]), reverse=False)
        human_keeps = {
            event_id for event_id, row in labels.items() if row["interpretation"] == "keep"
        }
        indexed_ids = {value["event_id"] for value in records}
        if not human_keeps.issubset(indexed_ids):
            raise ArchiveError("Knowledge index omitted a human keep")
        labeled_non_sensitive = [row for row in labels.values() if row["interpretation"] != "sensitive"]
        actual_keeps = {str(row["event_id"]) for row in labeled_non_sensitive if row["interpretation"] == "keep"}
        predicted = {str(row["event_id"]) for row in labeled_non_sensitive if str(row["event_id"]) in heuristic_labeled_predictions}
        matched = len(actual_keeps & predicted)
        validation = {
            "labeled_records": len(labeled_non_sensitive),
            "actual_keeps": len(actual_keeps),
            "predicted_keeps": len(predicted),
            "matched_keeps": matched,
            "sample_precision": matched / len(predicted) if predicted else None,
            "sample_recall": matched / len(actual_keeps) if actual_keeps else None,
        }
    finally:
        connection.close()
    document = {
        "schema": "things-verbatim-knowledge-index/v1",
        "source_sha256": source_hash,
        "created_at": support.now_utc(),
        "policy": "Human labels override; otherwise high-recall deterministic decision/insight/fact/date signals. Exact source only.",
        "validation": validation,
        "records": records,
    }
    private_write(json_path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    embedded = json.dumps(document, ensure_ascii=False).replace("</", "<\\/")
    html = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="referrer" content="no-referrer"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Things verbatim knowledge index</title><style>
:root{--bg:#f2f0eb;--card:#fff;--ink:#24221f;--muted:#6e6961;--line:#d8d2c8;--accent:#285eaf}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}header{position:sticky;top:0;background:#f2f0ebf5;border-bottom:1px solid var(--line);z-index:2}.controls{max-width:1150px;margin:auto;padding:12px 18px}.row{display:flex;gap:9px;flex-wrap:wrap;align-items:center}input[type=search]{flex:1;min-width:260px;padding:10px;border:1px solid var(--line);border-radius:8px}main{max-width:1150px;margin:auto;padding:18px 18px 80px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:12px 0}.meta{color:var(--muted);font-size:13px}.tag{display:inline-block;background:#e8edf7;border-radius:999px;padding:2px 8px;margin:2px;font-size:12px}.source{white-space:pre-wrap;overflow-wrap:anywhere;background:#f8f6f2;padding:12px;border-radius:8px;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;max-height:420px;overflow:auto}.load{padding:9px 13px}label{white-space:nowrap}
</style></head><body><header><div class="controls"><div class="row"><input id="search" type="search" placeholder="Search exact Things text…"><label><input type="checkbox" class="cat" value="decision"> decision</label><label><input type="checkbox" class="cat" value="insight"> insight</label><label><input type="checkbox" class="cat" value="registered_fact"> fact</label><label><input type="checkbox" class="cat" value="dated_event_fact"> dated event</label><label><input type="checkbox" id="strong"> human/strong only</label></div><p id="summary" class="meta"></p></div></header><main><div id="results"></div><button id="more" class="load">Load more</button><p class="meta">Private local archive. Exact source only; heuristic categories are retrieval hints, not claims. Nothing is uploaded.</p></main><script id="payload" type="application/json">__DATA__</script><script>
const data=JSON.parse(document.getElementById('payload').textContent),search=document.getElementById('search'),results=document.getElementById('results'),summary=document.getElementById('summary'),more=document.getElementById('more');let limit=100,filtered=[];function node(tag,text,cls){const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e}function apply(){const q=search.value.trim().toLocaleLowerCase(),cats=[...document.querySelectorAll('.cat:checked')].map(x=>x.value),strong=document.getElementById('strong').checked;filtered=data.records.filter(r=>(!q||[r.title,r.notes,r.checklist,r.context.join(' ')].join('\\n').toLocaleLowerCase().includes(q))&&(!cats.length||cats.every(x=>r.categories.includes(x)))&&(!strong||r.tier!=='broad'));limit=100;render()}function render(){results.replaceChildren();for(const r of filtered.slice(0,limit)){const card=node('article',undefined,'card'),head=node('div');for(const value of [r.tier,...r.categories])head.append(node('span',value,'tag'));card.append(head,node('h2',r.title),node('p',`${r.date||'no date'} · ${r.status} · ${r.context.join(' / ')||'No context'} · score ${r.score}`,'meta'));const details=node('details'),label=node('summary','Show exact source');details.append(label,node('pre',[r.notes,r.checklist].filter(Boolean).join('\\n\\nChecklist:\\n')||'(Title and completion date are the complete record.)','source'));card.append(details);const link=node('a','Open in Things');link.href=r.things_url;card.append(link);results.append(card)}summary.textContent=`${filtered.length} matching records · showing ${Math.min(limit,filtered.length)} · ${data.validation.sample_recall*100}% recall on labeled sample`;more.hidden=limit>=filtered.length}search.oninput=apply;document.querySelectorAll('.cat,#strong').forEach(x=>x.onchange=apply);more.onclick=()=>{limit+=100;render()};apply();
</script></body></html>""".replace("__DATA__", embedded)
    private_write(html_path, html)
    return {
        "records": len(records),
        "tiers": dict(sorted(Counter(value["tier"] for value in records).items())),
        "categories": dict(sorted(Counter(category for value in records for category in value["categories"]).items())),
        "validation": validation,
        "json_sha256": support.file_sha256(json_path),
        "html_sha256": support.file_sha256(html_path),
        "labeled_drops_excluded": labeled_drop,
        "labeled_sensitive_excluded": labeled_sensitive,
        "labeled_keeps_included": labeled_keep,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    commands = value.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("stage")
    stage.add_argument("--db", required=True, type=Path)
    stage.add_argument("--out", required=True, type=Path)
    profile = commands.add_parser("profile")
    profile.add_argument("--archive", required=True, type=Path)
    import_command = commands.add_parser("import-labels")
    import_command.add_argument("--archive", required=True, type=Path)
    import_command.add_argument("--labels", required=True, type=Path)
    report = commands.add_parser("label-report")
    report.add_argument("--archive", required=True, type=Path)
    report.add_argument("--sample", required=True, type=Path)
    index = commands.add_parser("build-index")
    index.add_argument("--archive", required=True, type=Path)
    index.add_argument("--json", required=True, type=Path)
    index.add_argument("--html", required=True, type=Path)
    review = commands.add_parser("review")
    review.add_argument("--archive", required=True, type=Path)
    review.add_argument("--sample-out", required=True, type=Path)
    review.add_argument("--out", required=True, type=Path)
    review.add_argument("--count", type=int, default=60)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "stage":
            archive = create_archive(args.db.resolve(), args.out.resolve())
            result = {"archive": str(archive), **archive_profile(archive)}
        elif args.command == "profile":
            result = archive_profile(args.archive.resolve())
        elif args.command == "import-labels":
            result = import_labels(args.archive.resolve(), args.labels.resolve())
        elif args.command == "label-report":
            result = label_report(args.archive.resolve(), args.sample.resolve())
        elif args.command == "build-index":
            result = build_knowledge_index(
                args.archive.resolve(), args.json.resolve(), args.html.resolve()
            )
        else:
            result = write_review(args.archive.resolve(), args.sample_out.resolve(), args.out.resolve(), args.count)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ArchiveError, support.ArchiveSourceError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
