"""Offline, no-provider tests of read-only daily journal progress attachment."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from mb_market_data.overlay_live_attachment import ReadOnlySessionObserver
from mb_market_data.overlay_session_catalog import SessionDiscoveryError


DAY = "2026-11-24"
T0 = datetime(2026, 11, 24, 14, 30, tzinfo=timezone.utc)


def utc_text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


class TestReadOnlySessionObserver(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "output" / "daily_session" / DAY
        self.state.mkdir(parents=True)
        self.journal = (
            self.root / "output" / f"quote_observation_journal_v2_api_opening_{DAY}"
            / f"{DAY}.sqlite3"
        )
        self.journal.parent.mkdir(parents=True)
        (self.state / "plan.json").write_text(json.dumps({
            "contract": "one-unlock-daily-flow-v1",
            "session_date": DAY,
            "data_root": str(self.root),
            "state_root": str(self.state),
            "journal": str(self.journal),
        }), encoding="utf-8")
        (self.state / "status.json").write_text(json.dumps({
            "session_date": DAY, "phase": "COLLECTING",
            "at": "2026-11-24T09:30:00-05:00", "pid": 12345,
        }), encoding="utf-8")
        with closing(sqlite3.connect(self.journal)) as connection, connection:
            connection.execute("PRAGMA user_version=2")
            connection.execute(
                "CREATE TABLE store_identity (singleton INTEGER, schema_version INTEGER, "
                "session_date TEXT, membership_contract TEXT)"
            )
            connection.execute(
                "INSERT INTO store_identity VALUES (1,2,?,?)",
                (DAY, "nested-uni-focus-hot-v1"),
            )
            connection.execute(
                "CREATE TABLE quote_acquisition "
                "(acquisition_id TEXT PRIMARY KEY, session_date TEXT, channel TEXT, "
                "completed_at_utc TEXT, requested_symbol_count INTEGER)"
            )
            connection.execute(
                "CREATE TABLE quote_observation (acquisition_id TEXT, symbol TEXT)"
            )
            connection.execute(
                "CREATE TABLE sampling_membership_revision "
                "(session_date TEXT, revision INTEGER, effective_at_utc TEXT)"
            )

    def write_acquisition(self, key: str, moment: datetime, *, expected: int = 2,
                          actual: int = 2) -> None:
        with closing(sqlite3.connect(self.journal)) as connection, connection:
            connection.execute(
                "INSERT INTO quote_acquisition VALUES (?,?,?,?,?)",
                (key, DAY, "focus", utc_text(moment), expected),
            )
            for i in range(actual):
                connection.execute(
                    "INSERT INTO quote_observation VALUES (?,?)",
                    (key, f"STOCK{i}"),
                )

    def test_finds_only_committed_new_acquisitions_without_live_process_claim(self):
        observer = ReadOnlySessionObserver(self.root, DAY)
        first = observer.observe(now=T0)
        self.assertEqual(first.committed_acquisitions, 0)
        self.assertIsNone(first.new_acquisitions_since_previous_check)
        self.assertEqual(first.controller_phase_saved, "COLLECTING")
        self.assertFalse(first.live_process_verified)
        self.write_acquisition("batch-1", T0 + timedelta(seconds=2))
        second = observer.observe(now=T0 + timedelta(seconds=5))
        self.assertEqual(second.committed_acquisitions, 1)
        self.assertEqual(second.new_acquisitions_since_previous_check, 1)
        self.assertEqual(second.latest_acquisition_id, "batch-1")
        self.assertEqual(
            second.latest_acquisition_completed_at_utc,
            utc_text(T0 + timedelta(seconds=2)),
        )
        third = observer.observe(now=T0 + timedelta(seconds=8))
        self.assertEqual(third.new_acquisitions_since_previous_check, 0)
        self.assertEqual(third.journal_path, str(self.journal))

    def test_future_effective_membership_is_not_prematurely_shown(self):
        with closing(sqlite3.connect(self.journal)) as connection, connection:
            connection.execute(
                "INSERT INTO sampling_membership_revision VALUES (?,?,?)",
                (DAY, 1, utc_text(T0 + timedelta(minutes=10))),
            )
        observer = ReadOnlySessionObserver(self.root, DAY)
        early = observer.observe(now=T0)
        self.assertEqual(early.effective_membership_revisions, 0)
        self.assertIsNone(early.latest_effective_revision)
        later = observer.observe(now=T0 + timedelta(minutes=11))
        self.assertEqual(later.effective_membership_revisions, 1)
        self.assertEqual(later.latest_effective_revision, 1)

    def test_rejects_incomplete_newest_batch_without_repairing_journal(self):
        self.write_acquisition("incomplete", T0, expected=2, actual=1)
        before = self.journal.stat().st_size
        with self.assertRaisesRegex(SessionDiscoveryError, "incomplete quote outcomes"):
            ReadOnlySessionObserver(self.root, DAY).observe(now=T0)
        self.assertEqual(self.journal.stat().st_size, before)

    def test_rejects_observed_acquisition_count_regression(self):
        self.write_acquisition("batch-1", T0)
        observer = ReadOnlySessionObserver(self.root, DAY)
        self.assertEqual(observer.observe(now=T0).committed_acquisitions, 1)
        with closing(sqlite3.connect(self.journal)) as connection, connection:
            connection.execute("DELETE FROM quote_observation")
            connection.execute("DELETE FROM quote_acquisition")
        with self.assertRaisesRegex(SessionDiscoveryError, "count decreased"):
            observer.observe(now=T0 + timedelta(seconds=1))

    def test_rejects_wrong_plan_identity_and_naive_observer_clock(self):
        observer = ReadOnlySessionObserver(self.root, DAY)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            observer.observe(now=datetime(2026, 11, 24, 9, 30))
        plan = self.state / "plan.json"
        original = json.loads(plan.read_text(encoding="utf-8"))
        original["journal"] = str(self.root / "another.sqlite3")
        plan.write_text(json.dumps(original), encoding="utf-8")
        with self.assertRaisesRegex(SessionDiscoveryError, "Plan journal differs"):
            observer.observe(now=T0)


if __name__ == "__main__":
    unittest.main()
