"""Minimal standalone Things snapshot reader used by the verbatim archive."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

STATUS = {0: "open", 2: "canceled", 3: "completed"}
KIND = {0: "todo", 1: "project", 2: "heading"}
URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
INSIGHT_MARKER_RE = re.compile(
    r"\b(?:because|therefore|realized|learned|turns out|conclusion|in practice|however|"
    r"failed|failure|root cause|потому что|поэтому|оказал\w*|выяснил\w*|понял\w*|"
    r"вывод\w*|на деле|на практике|не получил\w*|не получилось|причин\w*|проблем\w*)\b",
    re.I,
)
DECISION_MARKER_RE = re.compile(
    r"\b(?:decision|rationale|trade.?off|pros? and cons?|option|strategy|decided|chose|"
    r"решил\w*|выбрал\w*|вариант\w*|стратег\w*|обоснован\w*|компромисс\w*|"
    r"плюс\w*|минус\w*|не стоит)\b",
    re.I,
)
SPEC_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:₽|руб(?:лей|ля|ль)?|%|мм|см|кг|г|мл|л|mg|мг|gb|mb|"
    r"лет|год(?:а|ов)?|дн(?:я|ей)|час(?:а|ов)?)\b",
    re.I,
)
SENSITIVE_RE = re.compile(
    r"\b(?:password|passwd|пароль|api[_ -]?key|secret|access[_ -]?token|auth[_ -]?token|"
    r"private[_ -]?key|otp|снилс|паспорт|код (?:для )?(?:получения|доступа))\b|"
    r"-----BEGIN [A-Z ]+PRIVATE KEY-----|"
    r"(?<!\d)(?:\+7|8)[\s()\-]*\d{3}[\s()\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)",
    re.I,
)


class ArchiveSourceError(RuntimeError):
    pass


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def iso_epoch(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return dt.datetime.fromtimestamp(float(value), dt.timezone.utc).isoformat()
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    value = URL_RE.sub(" <url> ", value)
    value = re.sub(r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b", " <date> ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" .,:;!?-_()[]{}")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_ro(path: Path) -> sqlite3.Connection:
    quoted = str(path).replace("?", "%3f").replace("#", "%23")
    connection = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def require_schema(connection: sqlite3.Connection) -> None:
    required = {
        "TMTask": {"uuid", "type", "status", "trashed", "title", "notes", "stopDate"},
        "TMArea": {"uuid", "title"},
        "TMChecklistItem": {"uuid", "task", "title", "status", "leavesTombstone"},
        "TMTag": {"uuid", "title"},
        "TMTaskTag": {"tasks", "tags"},
    }
    problems: list[str] = []
    for table, columns in required.items():
        actual = {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}
        missing = columns - actual
        if missing:
            problems.append(f"{table}: missing {', '.join(sorted(missing))}")
    if problems:
        raise ArchiveSourceError("Unsupported Things schema: " + "; ".join(problems))


def fetch_tags(connection: sqlite3.Connection) -> dict[str, list[str]]:
    tags: dict[str, list[str]] = defaultdict(list)
    query = """
        SELECT link.tasks task_id, tag.title
        FROM TMTaskTag link JOIN TMTag tag ON tag.uuid=link.tags
        ORDER BY tag.title
    """
    for row in connection.execute(query):
        if row["title"]:
            tags[str(row["task_id"])].append(str(row["title"]))
    return tags


def fetch_checklists(connection: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    values: dict[str, list[dict[str, Any]]] = defaultdict(list)
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(TMChecklistItem)")}
    creation = "creationDate" if "creationDate" in columns else "NULL"
    stopped = "stopDate" if "stopDate" in columns else "NULL"
    index = '"index"' if "index" in columns else "0"
    query = f"""
        SELECT task, uuid, title, status, {creation} creationDate, {stopped} stopDate,
               {index} itemIndex
        FROM TMChecklistItem
        WHERE IFNULL(leavesTombstone, 0)=0
        ORDER BY task, itemIndex, uuid
    """
    for row in connection.execute(query):
        values[str(row["task"])].append(
            {
                "id": str(row["uuid"]),
                "title": str(row["title"] or ""),
                "status": STATUS.get(int(row["status"] or 0), f"unknown:{row['status']}"),
                "created_at": iso_epoch(row["creationDate"]),
                "stopped_at": iso_epoch(row["stopDate"]),
            }
        )
    return values


def task_columns(connection: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in connection.execute("PRAGMA table_info(TMTask)")}


def optional_column(columns: set[str], expression: str, alias: str) -> str:
    name = expression.split(".")[-1].strip('"')
    return f"{expression} AS {alias}" if name in columns else f"NULL AS {alias}"


def fetch_source_events(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    columns = task_columns(connection)
    tags = fetch_tags(connection)
    checklists = fetch_checklists(connection)
    optional = ",\n        ".join(
        [
            optional_column(columns, "t.creationDate", "creationDate"),
            optional_column(columns, "t.userModificationDate", "userModificationDate"),
            optional_column(columns, "t.rt1_repeatingTemplate", "repeatingTemplate"),
            optional_column(columns, 't."index"', "itemIndex"),
        ]
    )
    query = f"""
      SELECT t.uuid, t.type, t.status, t.trashed, t.title, t.notes, t.stopDate,
        t.area directArea, t.project directProject, t.heading headingID,
        {optional},
        h.title headingTitle, h.project headingProject, h.status headingStatus,
        h.trashed headingTrashed,
        p.uuid resolvedProjectID, p.title resolvedProjectTitle,
        p.status resolvedProjectStatus, p.trashed resolvedProjectTrashed,
        a.uuid resolvedAreaID, a.title resolvedAreaTitle,
        template.title repeatingTemplateTitle
      FROM TMTask t
      LEFT JOIN TMTask h ON h.uuid=t.heading
      LEFT JOIN TMTask p ON p.uuid=COALESCE(t.project,h.project)
      LEFT JOIN TMArea a ON a.uuid=COALESCE(t.area,p.area)
      LEFT JOIN TMTask template ON template.uuid=t.rt1_repeatingTemplate
      WHERE
        (t.type IN (0,1) AND t.status IN (2,3) AND IFNULL(t.trashed,0)=0)
        OR
        (t.type=0 AND t.status=0 AND IFNULL(t.trashed,0)=0 AND
          (h.status IN (2,3) OR IFNULL(h.trashed,0)!=0 OR
           p.status IN (2,3) OR IFNULL(p.trashed,0)!=0))
      ORDER BY IFNULL(t.stopDate,t.creationDate), t.uuid
    """
    events: list[dict[str, Any]] = []
    for row in connection.execute(query):
        event_id = str(row["uuid"])
        role = "finished" if int(row["status"] or 0) in (2, 3) else "archived_unfinished_child"
        events.append(
            {
                "id": event_id,
                "role": role,
                "kind": KIND.get(int(row["type"] or 0), f"unknown:{row['type']}"),
                "status": STATUS.get(int(row["status"] or 0), f"unknown:{row['status']}"),
                "title": str(row["title"] or ""),
                "notes": str(row["notes"] or ""),
                "created_at": iso_epoch(row["creationDate"]),
                "modified_at": iso_epoch(row["userModificationDate"]),
                "stopped_at": iso_epoch(row["stopDate"]),
                "area": {
                    "id": str(row["resolvedAreaID"]) if row["resolvedAreaID"] else None,
                    "title": str(row["resolvedAreaTitle"]) if row["resolvedAreaTitle"] else None,
                },
                "project": {
                    "id": str(row["resolvedProjectID"]) if row["resolvedProjectID"] else None,
                    "title": str(row["resolvedProjectTitle"]) if row["resolvedProjectTitle"] else None,
                    "status": STATUS.get(int(row["resolvedProjectStatus"] or 0))
                    if row["resolvedProjectID"] else None,
                },
                "heading": {
                    "id": str(row["headingID"]) if row["headingID"] else None,
                    "title": str(row["headingTitle"]) if row["headingTitle"] else None,
                },
                "repeat": {
                    "template_id": str(row["repeatingTemplate"]) if row["repeatingTemplate"] else None,
                    "template_title": str(row["repeatingTemplateTitle"])
                    if row["repeatingTemplateTitle"] else None,
                },
                "tags": tags.get(event_id, []),
                "checklist": checklists.get(event_id, []),
                "item_index": row["itemIndex"],
            }
        )
    return events


def event_content_hash(event: dict[str, Any]) -> str:
    payload = {
        "title": event["title"],
        "notes": event["notes"],
        "checklist": [
            {"title": item["title"], "status": item["status"]}
            for item in event["checklist"]
        ],
    }
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))
