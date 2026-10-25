"""SQLite storage: one table per data set, typed columns from the Garmin Swagger spec.

Every data table has:
  _key        primary key (usually the calendar date, see _catalog)
  <fields>    top-level fields of the Garmin response, typed; nested objects/arrays as JSON text
  raw_json    the full item exactly as Garmin returned it
  fetched_at  UTC time of the download
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from typing import Any

from .datasets import ACTIVITY_DETAIL, ACTIVITY_WORKOUTS, DATASETS, Dataset

SPEC_FILE = Path(__file__).with_name("openapi.json")
_SQL_TYPES = {"integer": "INTEGER", "number": "REAL", "boolean": "INTEGER", "string": "TEXT"}


@cache
def spec() -> dict:
    return json.loads(SPEC_FILE.read_text())


def _resolve(prop: dict) -> dict:
    if "allOf" in prop:
        prop = prop["allOf"][0]
    if "$ref" in prop:
        return {**spec()["components"]["schemas"][prop["$ref"].split("/")[-1]],
                "description": prop.get("description")}
    return prop


def columns(schema: str | None) -> dict[str, tuple[str, str]]:
    """Map field name to (SQL type, description) for a Swagger schema."""
    if schema is None:
        return {}
    props = spec()["components"]["schemas"][schema].get("properties", {})
    out = {}
    for name, prop in props.items():
        resolved = _resolve(prop)
        sql = _SQL_TYPES.get(resolved.get("type"), "JSON")
        out[name] = (sql, prop.get("description") or resolved.get("description") or "")
    return out


def _endpoint_description(ds: Dataset) -> str:
    shape = re.sub(r"\{\w+\}", "{}", ds.path)
    path = next((v for p, v in spec()["paths"].items() if re.sub(r"\{\w+\}", "{}", p) == shape), {})
    return (path.get("get") or {}).get("description") or ""


def connect(db_file: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_file)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL,
            dataset TEXT NOT NULL,
            start_date TEXT,
            end_date TEXT,
            status TEXT NOT NULL,          -- ok | failed
            rows INTEGER,
            error TEXT,
            finished_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS sync_log_dataset ON sync_log(dataset, status);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS activity_file (
            _key TEXT PRIMARY KEY,         -- activityId
            zip BLOB,                      -- original upload (FIT/TCX/GPX) zipped; NULL if Garmin has none
            fetched_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS _catalog (
            table_name TEXT NOT NULL,
            column_name TEXT,              -- NULL row describes the table
            type TEXT,
            description TEXT
        );
    """)
    for ds in [*DATASETS, ACTIVITY_DETAIL, ACTIVITY_WORKOUTS]:
        _ensure_table(conn, ds)
    _write_catalog(conn)
    conn.commit()
    return conn


def _ensure_table(conn: sqlite3.Connection, ds: Dataset) -> None:
    cols = columns(ds.schema)
    conn.execute(f'CREATE TABLE IF NOT EXISTS "{ds.name}" '
                 "(_key TEXT PRIMARY KEY, raw_json TEXT NOT NULL, fetched_at TEXT NOT NULL)")
    existing = {r[1] for r in conn.execute(f'PRAGMA table_info("{ds.name}")')}
    for name, (sql, _) in cols.items():
        if name not in existing:
            conn.execute(f'ALTER TABLE "{ds.name}" ADD COLUMN "{name}" {sql}')


def _write_catalog(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM _catalog")
    rows = [
        ("sync_log", None, None, "One row per download attempt. Dates covered by status=ok rows are not downloaded again (except the last 7 days)."),
        ("meta", None, None, "Backup settings, e.g. origin_date = first day of the backup."),
        ("activity_file", None, None, "Original activity file as ZIP bytes, key = activityId. Join activities._key."),
        ("activity_workouts", None, None, "Workouts of one activity, key = activityId; raw_json is the Garmin array."),
    ]
    for ds in [*DATASETS, ACTIVITY_DETAIL]:
        rows.append((ds.name, None, None,
                     f"{_endpoint_description(ds)} Source: GET {ds.path}. Schema: {ds.schema}."))
        rows += [(ds.name, n, t, d) for n, (t, d) in columns(ds.schema).items()]
    rows.append(("*", "_key", "TEXT", "Primary key: calendar date (yyyy-MM-dd) for daily tables, activityId for activity tables."))
    rows.append(("*", "raw_json", "TEXT", "Full item exactly as returned by Garmin. Use json_extract()/json_each()."))
    rows.append(("*", "JSON columns", "JSON", "Nested Garmin objects/arrays stored as JSON text."))
    conn.executemany("INSERT INTO _catalog VALUES (?, ?, ?, ?)", rows)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cell(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return int(value) if isinstance(value, bool) else value


def upsert(conn: sqlite3.Connection, ds: Dataset, key: str, item: Any) -> None:
    cols = [c for c in columns(ds.schema) if isinstance(item, dict) and c in item]
    names = ", ".join(f'"{c}"' for c in ["_key", "raw_json", "fetched_at", *cols])
    values = [key, json.dumps(item, ensure_ascii=False), _now(), *(_cell(item[c]) for c in cols)]
    conn.execute(f'INSERT OR REPLACE INTO "{ds.name}" ({names}) VALUES ({", ".join("?" * len(values))})',
                 values)


def has_row(conn: sqlite3.Connection, table: str, key: str) -> bool:
    return conn.execute(f'SELECT 1 FROM "{table}" WHERE _key = ?', (key,)).fetchone() is not None


def save_file(conn: sqlite3.Connection, activity_id: str, data: bytes | None) -> None:
    conn.execute("INSERT OR REPLACE INTO activity_file VALUES (?, ?, ?)", (activity_id, data, _now()))


def log(conn: sqlite3.Connection, run_id: str, dataset: str, start: str | None, end: str | None,
        status: str, rows: int | None = None, error: str | None = None) -> None:
    conn.execute("INSERT INTO sync_log (run_id, dataset, start_date, end_date, status, rows, error, finished_at)"
                 " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (run_id, dataset, start, end, status, rows, error, _now()))


def covered_days(conn: sqlite3.Connection, dataset: str) -> set[date]:
    """Every day already downloaded with status ok for this data set."""
    days: set[date] = set()
    for start, end in conn.execute(
            "SELECT start_date, end_date FROM sync_log WHERE dataset = ? AND status = 'ok' "
            "AND start_date IS NOT NULL", (dataset,)):
        d, last = date.fromisoformat(start), date.fromisoformat(end)
        while d <= last:
            days.add(d)
            d += timedelta(days=1)
    return days


def missing_days(conn: sqlite3.Connection, ds: Dataset, first: date, last: date) -> set[date]:
    """Days in first..last with no row in the data set's table (a day Garmin has no data for counts too)."""
    have = {r[0] for r in conn.execute(f'SELECT _key FROM "{ds.name}"')}
    days = (first + timedelta(days=i) for i in range((last - first).days + 1))
    return {d for d in days if d.isoformat() not in have}


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
