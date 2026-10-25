import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from garmin_personal_api import HttpError

from garmin_data_handler import sync

D = date.fromisoformat


class PlanTest(unittest.TestCase):
    def test_full_history_split_by_max_days(self):
        chunks = sync.plan(D("2026-01-01"), D("2026-01-10"), set(), 4, refetch_days=1)
        self.assertEqual(chunks, [(D("2026-01-01"), D("2026-01-04")),
                                  (D("2026-01-05"), D("2026-01-08")),
                                  (D("2026-01-09"), D("2026-01-10"))])

    def test_only_gaps_and_refetch_window(self):
        covered = {D("2026-01-01") + timedelta(days=i) for i in range(10)} - {D("2026-01-03")}
        chunks = sync.plan(D("2026-01-01"), D("2026-01-10"), covered, 28, refetch_days=2)
        self.assertEqual(chunks, [(D("2026-01-03"), D("2026-01-03")),
                                  (D("2026-01-09"), D("2026-01-10"))])


class FakeApi:
    """Answers every endpoint with one small item; fails steps once."""

    def __init__(self, person_dir, api_key):
        self.calls = []
        self.fail_steps = True

    def get(self, path, params=None):
        self.calls.append(path)
        p = params or {}
        if "activities/search" in path:
            if p.get("sortOrder") == "asc":
                return [{"startTimeLocal": "2026-01-01 08:00:00"}]
            return [{"activityId": 7, "activityName": "Run"}] if p["start"] == 0 else []
        if "steps/daily" in path:
            if self.fail_steps:
                self.fail_steps = False
                raise HttpError(status_code=400, method="GET", url=path, response_text="bad")
            return [{"calendarDate": path.split("/")[-2], "totalSteps": 1000}]
        if "dailysummaries" in path:
            if p["startDate"] < "2026-01-01":
                return {"data": []}
            if p["startDate"] != p["endDate"]:
                raise HttpError(status_code=500, method="GET", url=path, response_text="")
            return {"data": [{"calendarDate": p["startDate"]}]}
        if "/hrv-service/hrv/2" in path:
            return {"hrvSummary": {"weeklyAvg": 50}, "hrvReadings": [{"hrvValue": 40}]}
        return None

    def download(self, activity_id):
        return b"PK-zip"


class SyncPersonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.person = self.tmp / "anna"
        self.person.mkdir()

    def test_empty_person_root_stops(self):
        self.person.rmdir()
        self.assertEqual(sync.main(self.tmp), 1)

    def test_missing_key_creates_files_and_stops(self):
        res = sync.sync_person(self.person, api_factory=FakeApi)
        self.assertIn("needs API key", res.status)
        for name in ("api_key.txt", "sync.log", "garmin.sqlite", "datasets.txt"):
            self.assertTrue((self.person / name).exists(), name)
        self.assertEqual((self.person / "api_key.txt").stat().st_mode & 0o777, 0o600)

    def test_full_then_incremental(self):
        sync.prepare(self.person)
        (self.person / "api_key.txt").write_text("key\n")
        (self.person / "datasets.txt").write_text("steps_daily\nhrv_detail\n# spo2\nactivities\ndaily_summary\n")
        fake = FakeApi(None, None)
        today = D("2026-01-20")

        first = sync.sync_person(self.person, today=today, api_factory=lambda *a: fake)
        self.assertEqual(first.origin, "2026-01-01")
        self.assertEqual(first.failed, 1)  # first steps chunk
        self.assertFalse(any("spo2" in c for c in fake.calls))

        conn = sqlite3.connect(self.person / "garmin.sqlite")
        self.assertEqual(conn.execute("SELECT count(*) FROM hrv_detail").fetchone()[0], 20)
        self.assertEqual(conn.execute("SELECT json_extract(hrvSummary, '$.weeklyAvg') FROM hrv_detail LIMIT 1").fetchone()[0], 50)
        self.assertEqual(conn.execute("SELECT zip FROM activity_file WHERE _key='7'").fetchone()[0], b"PK-zip")
        self.assertGreater(conn.execute("SELECT count(*) FROM _catalog WHERE table_name='steps_daily'").fetchone()[0], 3)
        # 5xx ranges are split down to single days instead of failing
        self.assertEqual(conn.execute("SELECT count(*) FROM daily_summary").fetchone()[0], 20)

        fake.calls.clear()
        second = sync.sync_person(self.person, today=today, api_factory=lambda *a: fake)
        self.assertEqual(second.failed, 0)
        hrv_days = [c for c in fake.calls if c.startswith("/hrv-service/hrv/2")]
        self.assertEqual(len(hrv_days), sync.REFETCH_DAYS)  # only the refetch window
        steps = [c for c in fake.calls if "steps/daily" in c]
        self.assertEqual(steps, ["/usersummary-service/stats/steps/daily/2026-01-01/2026-01-20"])
        self.assertIn("FAIL steps_daily", (self.person / "sync.log").read_text())

    def test_missing_only_fills_gaps_before_today_and_writes_report(self):
        sync.prepare(self.person)
        (self.person / "api_key.txt").write_text("key\n")
        (self.person / "datasets.txt").write_text("hrv_detail\nactivities\n")
        (self.person / "start_date.txt").write_text("2026-01-01\n")
        fake = FakeApi(None, None)
        today = D("2026-01-20")
        sync.sync_person(self.person, today=today, api_factory=lambda *a: fake)
        conn = sqlite3.connect(self.person / "garmin.sqlite")
        conn.execute("DELETE FROM hrv_detail WHERE _key IN ('2026-01-05', '2026-01-06', '2026-01-20')")
        conn.commit()
        conn.close()

        fake.calls.clear()
        res = sync.sync_person(self.person, today=today, api_factory=lambda *a: fake, missing_only=True)
        self.assertEqual(res.missing, {"hrv_detail": (2, 0)})  # today is not a gap; activities are skipped
        self.assertEqual(sorted(fake.calls), ["/hrv-service/hrv/2026-01-05", "/hrv-service/hrv/2026-01-06"])
        report = sync.write_missing_report(self.tmp, [res]).read_text()
        self.assertIn("hrv_detail: 2 missing days, 2 retrieved, 0 still missing", report)


if __name__ == "__main__":
    unittest.main()
