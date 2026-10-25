"""Backup workflow: check person folders, plan missing dates, download, store, log."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from garmin_personal_api import GarminPersonalApiClient, HttpError, PersonalApiAuthenticator

from . import db
from .datasets import ACTIVITY_DETAIL, ACTIVITY_WORKOUTS, BY_NAME, DATASETS, Dataset

API_KEY_FILE = "api_key.txt"
LOG_FILE = "sync.log"
MISSING_LOG_FILE = "missing_data.log"
DB_FILE = "garmin.sqlite"
DATASETS_FILE = "datasets.txt"
START_DATE_FILE = "start_date.txt"
TOKEN_FILE = "token.json"

REFETCH_DAYS = 7  # Garmin finalizes recent days later (sleep, HRV), so these are always downloaded again
PAGE_SIZE = 100
EARLIEST = date(2000, 1, 1)

log = logging.getLogger("garmin_data_handler")


class AuthError(Exception):
    """The API key or token is not accepted. Stop this person."""


# ---------- planning ----------

def plan(origin: date, today: date, covered: set[date], max_days: int,
         refetch_days: int = REFETCH_DAYS) -> list[tuple[date, date]]:
    """Return inclusive date chunks still to download, max ``max_days`` long."""
    window_start = today - timedelta(days=refetch_days - 1)
    chunks: list[tuple[date, date]] = []
    d = origin
    while d <= today:
        if d not in covered or d >= window_start:
            if chunks and (d - chunks[-1][1]).days == 1 and (d - chunks[-1][0]).days < max_days:
                chunks[-1] = (chunks[-1][0], d)
            else:
                chunks.append((d, d))
        d += timedelta(days=1)
    return chunks


# ---------- API with retry ----------

class Api:
    def __init__(self, person_dir: Path, api_key: str):
        self._key = api_key
        self._auth = PersonalApiAuthenticator(bearer_token_cache_file=person_dir / TOKEN_FILE)
        try:
            token = self._auth.obtain_token(api_key)
        except HttpError as e:
            raise AuthError(f"API key rejected: HTTP {e.status_code}") from e
        self.client = GarminPersonalApiClient(token.access_token)

    def _call(self, fn, *args, **kwargs) -> Any:
        refreshed = False
        for attempt in range(6):
            try:
                return fn(*args, **kwargs)
            except HttpError as e:
                if e.status_code == 401 and not refreshed:
                    refreshed = True
                    self.client.update_bearer_token(
                        self._auth.obtain_token(self._key, force_refresh=True).access_token)
                    continue
                if e.status_code == 401:
                    raise AuthError("HTTP 401 after token refresh") from e
                # 5xx: retry briefly, then the caller splits the range into smaller parts
                if (e.status_code == 429 and attempt < 5) or (e.status_code >= 500 and attempt < 2):
                    self._wait(attempt, f"HTTP {e.status_code}")
                    continue
                raise
            except requests.RequestException as e:
                if attempt < 5:
                    self._wait(attempt, type(e).__name__)
                    continue
                raise
        raise AssertionError("unreachable")

    @staticmethod
    def _wait(attempt: int, reason: str) -> None:
        seconds = min(300, 10 * 2 ** attempt)
        log.warning("%s, retry in %ss", reason, seconds)
        time.sleep(seconds)

    def get(self, path: str, params: dict | None = None) -> Any:
        try:
            return self._call(self.client.get, path, params=params or None)
        except HttpError as e:
            if e.status_code == 404:  # Garmin uses 404 for "no data for this day"
                return None
            raise

    def download(self, activity_id: str) -> bytes | None:
        try:
            return self._call(self.client.download_activity, activity_id)
        except HttpError as e:
            if e.status_code == 404:  # e.g. manual activity: no original file
                return None
            raise


# ---------- person folder ----------

def datasets_template() -> str:
    lines = ["# Garmin data sets to back up. Put # in front of a line to turn it off.",
             "# Turning a data set on later downloads its full history.", ""]
    lines += [f"{'' if d.default_on else '#'}{d.name:<20}# {d.cost}" if d.cost else d.name
              for d in DATASETS]
    return "\n".join(lines) + "\n"


def read_datasets(path: Path) -> list[Dataset]:
    enabled = []
    for line in path.read_text().splitlines():
        name = line.split("#", 1)[0].strip()
        if not name:
            continue
        if name not in BY_NAME:
            log.warning("Unknown data set in %s: %s", path.name, name)
            continue
        enabled.append(BY_NAME[name])
    return enabled


def prepare(person_dir: Path) -> str | None:
    """Create missing files. Return the API key, or None when it is still empty."""
    key_file = person_dir / API_KEY_FILE
    if not key_file.exists():
        key_file.touch(mode=0o600)
    key_file.chmod(0o600)
    (person_dir / LOG_FILE).touch()
    if not (person_dir / DATASETS_FILE).exists():
        (person_dir / DATASETS_FILE).write_text(datasets_template())
    db.connect(person_dir / DB_FILE).close()
    return key_file.read_text().strip() or None


def _setup_logging(person_dir: Path) -> None:
    for h in list(log.handlers):
        log.removeHandler(h)
        h.close()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for h in (logging.FileHandler(person_dir / LOG_FILE), logging.StreamHandler()):
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)
    log.propagate = False


# ---------- download ----------

def detect_origin(api: Api, today: date) -> date:
    """First day with data: oldest activity, or oldest daily summary found going back a year at a time."""
    found = today
    oldest = api.get(BY_NAME["activities"].path,
                     {"start": 0, "limit": 1, "sortOrder": "asc", "excludeChildren": "false"})
    if oldest:
        found = min(found, date.fromisoformat(oldest[0]["startTimeLocal"][:10]))
    end, empty_years = today, 0
    while empty_years < 2 and end > EARLIEST:
        start = end - timedelta(days=364)
        resp = api.get("/usersummary-service/v2/dailysummaries",
                       {"startDate": start.isoformat(), "endDate": end.isoformat()})
        days = [date.fromisoformat(x["calendarDate"]) for x in (resp or {}).get("data") or []
                if x.get("includesWellnessData") or x.get("includesActivityData")]
        if days:
            found, empty_years = min(found, min(days)), 0
        else:
            empty_years += 1
        end = start - timedelta(days=1)
    return found


def fetch_chunk(api: Api, conn, ds: Dataset, start: date, end: date, today: date) -> int:
    values = {"start": start.isoformat(), "end": end.isoformat(),
              "date": start.isoformat(), "today": today.isoformat()}
    resp = api.get(ds.path.format(**values), {k: v.format(**values) for k, v in ds.params.items()})
    items = ds.rows(resp)
    for item in items:
        db.upsert(conn, ds, ds.key(item, start.isoformat()), item)
    return len(items)


def fetch_activities(api: Api, conn, ds: Dataset, start: date, end: date, refresh: bool) -> int:
    count, offset = 0, 0
    while True:
        items = api.get(ds.path, {"start": offset, "limit": PAGE_SIZE, "startDate": start.isoformat(),
                                  "endDate": end.isoformat(), "excludeChildren": "false"}) or []
        for item in items:
            aid = str(item["activityId"])
            db.upsert(conn, ds, aid, item)
            if refresh or not db.has_row(conn, ACTIVITY_DETAIL.name, aid):
                db.upsert(conn, ACTIVITY_DETAIL, aid, api.get(ACTIVITY_DETAIL.path.format(id=aid)))
                db.upsert(conn, ACTIVITY_WORKOUTS, aid,
                          api.get(ACTIVITY_WORKOUTS.path.format(id=aid)) or [])
            if not db.has_row(conn, "activity_file", aid):
                db.save_file(conn, aid, api.download(aid))
            conn.commit()  # keep finished activities if the run stops mid-chunk
            count += 1
        if len(items) < PAGE_SIZE:
            return count
        offset += PAGE_SIZE


@dataclass
class Result:
    person: str
    status: str = "ok"
    ok: int = 0
    failed: int = 0
    rows: int = 0
    origin: str = ""
    failures: list[str] = field(default_factory=list)
    # missing-data mode: data set -> (missing days found, still missing after the retrieval)
    missing: dict[str, tuple[int, int]] = field(default_factory=dict)


def sync_person(person_dir: Path, today: date | None = None, api_factory=Api,
                full: bool = False, missing_only: bool = False) -> Result:
    """``missing_only``: download only the days before today that have no row yet."""
    res = Result(person_dir.name)
    api_key = prepare(person_dir)
    if api_key is None:
        res.status = f"needs API key: put it in {person_dir / API_KEY_FILE} and run again"
        return res
    _setup_logging(person_dir)
    today = today or date.today()
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    conn = db.connect(person_dir / DB_FILE)
    try:
        log.info("=== run %s for %s ===", run_id, person_dir.name)
        api = api_factory(person_dir, api_key)
        start_file = person_dir / START_DATE_FILE
        if start_file.exists() and start_file.read_text().strip():
            origin = date.fromisoformat(start_file.read_text().strip())
        elif db.get_meta(conn, "origin_date"):
            origin = date.fromisoformat(db.get_meta(conn, "origin_date"))
        else:
            origin = detect_origin(api, today)
            db.set_meta(conn, "origin_date", origin.isoformat())
            conn.commit()
            log.info("Origin date detected: %s", origin)
        res.origin = origin.isoformat()
        window_start = today - timedelta(days=REFETCH_DAYS - 1)

        for ds in read_datasets(person_dir / DATASETS_FILE):
            gaps: set[date] = set()
            if missing_only:
                last = today - timedelta(days=1)
                if ds.kind not in ("range", "day") or ds.sparse:
                    continue
                gaps = db.missing_days(conn, ds, origin, last)
                if not gaps:
                    continue
                all_days = {origin + timedelta(days=i) for i in range((last - origin).days + 1)}
                chunks = plan(origin, last, all_days - gaps, ds.max_days, refetch_days=0)
            elif ds.kind == "once":
                chunks = [(today, today)]
            else:
                chunks = plan(origin, today, set() if full else db.covered_days(conn, ds.name), ds.max_days)
            while chunks:
                start, end = chunks.pop(0)
                try:
                    if ds.kind == "activities":
                        n = fetch_activities(api, conn, ds, start, end, refresh=full or end >= window_start)
                    else:
                        n = fetch_chunk(api, conn, ds, start, end, today)
                except AuthError:
                    raise
                except Exception as e:
                    conn.rollback()
                    # Garmin returns 5xx for some large ranges that work in smaller parts
                    if isinstance(e, HttpError) and e.status_code >= 500 and start < end:
                        mid = start + (end - start) // 2
                        chunks[:0] = [(start, mid), (mid + timedelta(days=1), end)]
                        log.warning("SPLIT %s %s..%s after HTTP %s", ds.name, start, end, e.status_code)
                        continue
                    db.log(conn, run_id, ds.name, start.isoformat(), end.isoformat(), "failed", error=str(e)[:2000])
                    conn.commit()
                    res.failed += 1
                    res.failures.append(f"{ds.name} {start}..{end}: {str(e)[:200]}")
                    log.warning("FAIL %s %s..%s: %s", ds.name, start, end, e)
                    continue
                db.log(conn, run_id, ds.name, start.isoformat(), end.isoformat(), "ok", rows=n)
                conn.commit()
                res.ok += 1
                res.rows += n
                log.info("OK   %s %s..%s rows=%d", ds.name, start, end, n)
            if missing_only:
                res.missing[ds.name] = (len(gaps), len(db.missing_days(conn, ds, origin, last)))
                log.info("MISSING %s: %d days without data, %d left after retrieval",
                         ds.name, *res.missing[ds.name])
    except AuthError as e:
        res.status = f"auth failed: {e}"
        log.error("STOP %s: %s", person_dir.name, e)
    finally:
        conn.close()
    log.info("=== done: ok=%d failed=%d rows=%d ===", res.ok, res.failed, res.rows)
    return res


def write_missing_report(person_root: Path, results: list[Result]) -> Path:
    """Append the outcome of a missing-data retrieval to one log file in the person root."""
    path = person_root / MISSING_LOG_FILE
    with path.open("a") as f:
        f.write(f"=== missing data retrieval {datetime.now().isoformat(timespec='seconds')} ===\n")
        for r in results:
            f.write(f"{r.person}: {r.status} | requests ok={r.ok} failed={r.failed} rows={r.rows}\n")
            for name, (found, left) in r.missing.items():
                f.write(f"  {name}: {found} missing days, {found - left} retrieved, {left} still missing\n")
            if not r.missing and r.status == "ok":
                f.write("  no missing days\n")
            for fail in r.failures:
                f.write(f"  FAIL {fail}\n")
    return path


def main(person_root: Path, missing: bool = False) -> int:
    person_root.mkdir(parents=True, exist_ok=True)
    people = sorted(p for p in person_root.iterdir() if p.is_dir() and not p.name.startswith("."))
    if not people:
        print(f"No person folders in {person_root}. Create {person_root}/<name>/ and run again.")
        return 1
    if missing:
        print(f"Missing data report: {write_missing_report(person_root, [sync_person(p, missing_only=True) for p in people])}")
    results = [sync_person(p) for p in people]
    print("\nSummary:")
    for r in results:
        print(f"  {r.person}: {r.status} | ok={r.ok} failed={r.failed} rows={r.rows}"
              + (f" | data from {r.origin}" if r.origin else ""))
        for f in r.failures[:5]:
            print(f"    - {f}")
        if len(r.failures) > 5:
            print(f"    ... {len(r.failures) - 5} more, see sync.log")
    return 0 if all(r.status == "ok" and not r.failed for r in results) else 1
