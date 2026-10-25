import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from garmin_data_handler import cli, sync

from test_sync import FakeApi

APP = Path(__file__).resolve().parents[1]


class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 1, 20)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.fake = FakeApi(None, None)
        self.fake.fail_steps = False

    def run_cli(self, *argv):
        with mock.patch("builtins.print"):
            return cli.run(["--person-root", str(self.root), *argv], api_factory=lambda *a: self.fake)

    def person(self, name="anna"):
        return self.root / name

    def snapshot(self, d):
        return {p.name: p.read_bytes() for p in d.iterdir()}

    def test_new_creates_files(self):
        self.assertEqual(self.run_cli("new", "anna"), 0)
        d = self.person()
        self.assertEqual((d / "api_key.txt").stat().st_mode & 0o777, 0o600)
        self.assertTrue((d / "datasets.txt").exists())

    def test_new_rejects_existing_and_bad_names(self):
        self.assertEqual(self.run_cli("new", "anna"), 0)
        self.assertEqual(self.run_cli("new", "anna"), 2)
        for bad in ("../evil", ".hidden", "a/b"):
            self.assertEqual(self.run_cli("new", bad), 2, bad)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["anna"])

    def test_edit_key_datasets_start_date(self):
        self.run_cli("new", "anna")
        d = self.person()
        self.assertEqual(self.run_cli("edit", "anna", "--api-key", "dummy-key"), 0)
        self.assertEqual((d / "api_key.txt").read_text(), "dummy-key\n")
        self.assertEqual((d / "api_key.txt").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.run_cli("edit", "anna", "--datasets", "steps_daily, hrv_detail"), 0)
        self.assertEqual({x.name for x in sync.read_datasets(d / "datasets.txt")}, {"steps_daily", "hrv_detail"})
        self.assertEqual(self.run_cli("edit", "anna", "--start-date", "2025-03-04"), 0)
        self.assertEqual((d / "start_date.txt").read_text(), "2025-03-04\n")

    def test_edit_touches_only_what_is_asked(self):
        self.run_cli("new", "anna")
        d = self.person()
        before = self.snapshot(d)
        self.run_cli("edit", "anna", "--start-date", "2025-03-04")
        after = self.snapshot(d)
        self.assertEqual({k for k in after if after[k] != before.get(k)}, {"start_date.txt"})

    def test_edit_rejects_bad_input_without_changes(self):
        self.run_cli("new", "anna")
        d = self.person()
        before = self.snapshot(d)
        self.assertEqual(self.run_cli("edit", "anna", "--api-key", "dummy-key", "--datasets", "nope"), 2)
        self.assertEqual(self.run_cli("edit", "anna", "--api-key", "dummy-key", "--start-date", "2025-13-45"), 2)
        self.assertEqual(self.run_cli("edit", "ghost", "--start-date", "2025-01-01"), 2)
        self.assertEqual(self.snapshot(d), before)
        self.assertFalse(self.person("ghost").exists())

    def test_sync_without_key_fails_without_api(self):
        self.run_cli("new", "anna")
        self.assertNotEqual(self.run_cli("sync", "anna"), 0)
        self.assertEqual(self.fake.calls, [])
        self.assertNotEqual(self.run_cli("sync", "ghost"), 0)

    def test_sync_then_again_then_full(self):
        self.run_cli("new", "anna")
        self.run_cli("edit", "anna", "--api-key", "dummy-key", "--datasets", "hrv_detail",
                     "--start-date", "2026-01-10")
        hrv = lambda: [c for c in self.fake.calls if c.startswith("/hrv-service/hrv/2")]
        with mock.patch.object(sync, "date", FixedDate):
            self.assertEqual(self.run_cli("sync", "anna"), 0)
            self.assertEqual(len(hrv()), 11)  # 2026-01-10 .. 01-20
            self.fake.calls.clear()
            self.assertEqual(self.run_cli("sync", "anna"), 0)
            self.assertEqual(len(hrv()), sync.REFETCH_DAYS)  # covered days skipped
            self.fake.calls.clear()
            self.assertEqual(self.run_cli("sync", "anna", "--full"), 0)
            self.assertEqual(len(hrv()), 11)


class LegacyTest(unittest.TestCase):
    def test_single_path_argument_runs_legacy_main(self):
        root = tempfile.mkdtemp()
        r = subprocess.run([sys.executable, "-m", "garmin_data_handler", root], cwd=APP,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("No person folders", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()


class OnboardTest(unittest.TestCase):
    def test_creates_root_person_and_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "person"
            answers = iter(["../bad", "anna"])
            cli.onboard(root, ask=lambda _: next(answers), ask_secret=lambda _: "dummy-key")
            key = root / "anna" / "api_key.txt"
            self.assertEqual(key.read_text(), "dummy-key\n")
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)

    def test_empty_name_cancels_and_empty_key_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "person"
            cli.onboard(root, ask=lambda _: "", ask_secret=lambda _: "")
            self.assertEqual(list(root.iterdir()), [])
            cli.onboard(root, ask=lambda _: "bob", ask_secret=lambda _: "")
            self.assertEqual((root / "bob" / "api_key.txt").read_text(), "")
