"""Isolated, no-provider tests for the dated Overlay session attachment gate."""
from __future__ import annotations

from contextlib import closing
from datetime import date
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from mb_market_data.overlay_session_catalog import (
    SessionDiscoveryError, catalog_sessions, inspect_session,
)


DAY = "2026-11-24"
CONTRACT = "nested-uni-focus-hot-v1"


class TestOverlaySessionCatalog(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "output" / "daily_session" / DAY
        self.state.mkdir(parents=True)
        self.journal = self.root / "output" / f"quote_observation_journal_v2_api_opening_{DAY}" / f"{DAY}.sqlite3"
        self.journal.parent.mkdir(parents=True)
        self.plan = {
            "contract": "one-unlock-daily-flow-v1",
            "session_date": DAY,
            "data_root": str(self.root),
            "state_root": str(self.state),
            "journal": str(self.journal),
        }
        self.write_plan()
        self.write_journal()

    def write_plan(self):
        (self.state / "plan.json").write_text(json.dumps(self.plan), encoding="utf-8")

    def write_journal(self, *, user_version=2, contract=CONTRACT, day=DAY):
        self.journal.unlink(missing_ok=True)
        with closing(sqlite3.connect(self.journal)) as db, db:
            db.execute(f"PRAGMA user_version={user_version}")
            db.execute("CREATE TABLE store_identity (singleton INTEGER, schema_version INTEGER, session_date TEXT, membership_contract TEXT)")
            db.execute("INSERT INTO store_identity VALUES (1,?,?,?)", (user_version, day, contract))

    def test_validated_dated_session_and_discovery(self):
        (self.state / "status.json").write_text(json.dumps({
            "session_date": DAY, "phase": "COLLECTING",
            "at": "2026-11-24T09:32:00-05:00", "pid": 45896,
        }), encoding="utf-8")
        events = self.state / "nasdaq" / "events.json"
        events.parent.mkdir()
        events.write_text("{}", encoding="utf-8")
        session = inspect_session(self.root, date.fromisoformat(DAY))
        self.assertEqual(session.session_date, DAY)
        self.assertEqual(session.controller_phase_saved, "COLLECTING")
        self.assertEqual(session.status_saved_at, "2026-11-24T09:32:00-05:00")
        self.assertEqual(session.nasdaq_events_path, str(events))
        self.assertIsNone(session.polling_health_directory)
        sessions, rejected = catalog_sessions(self.root)
        self.assertEqual([x.session_date for x in sessions], [DAY])
        self.assertEqual(rejected, {})

    def test_missing_journal_rejected_without_recreation(self):
        self.journal.unlink()
        with self.assertRaisesRegex(SessionDiscoveryError, "No daily journal"):
            inspect_session(self.root, DAY)
        self.assertFalse(self.journal.exists())

    def test_plan_journal_path_mismatch_rejected(self):
        self.plan["journal"] = str(self.root / "different.sqlite3")
        self.write_plan()
        with self.assertRaisesRegex(SessionDiscoveryError, "Plan journal differs"):
            inspect_session(self.root, DAY)

    def test_wrong_schema_and_contract_rejected(self):
        self.write_journal(user_version=1)
        with self.assertRaisesRegex(SessionDiscoveryError, "schema version"):
            inspect_session(self.root, DAY)
        self.write_journal(contract="wrong-contract")
        with self.assertRaisesRegex(SessionDiscoveryError, "Journal identity"):
            inspect_session(self.root, DAY)

    def test_wrong_journal_date_rejected(self):
        self.write_journal(day="2026-11-23")
        with self.assertRaisesRegex(SessionDiscoveryError, "Journal identity"):
            inspect_session(self.root, DAY)

    def test_status_for_other_date_is_not_trusted(self):
        (self.state / "status.json").write_text(json.dumps({
            "session_date": "2026-11-23", "phase": "COLLECTING", "pid": 12,
        }), encoding="utf-8")
        with self.assertRaisesRegex(SessionDiscoveryError, "different session"):
            inspect_session(self.root, DAY)

    def test_rejected_candidate_is_visible_not_listed_as_available(self):
        self.plan["contract"] = "wrong"
        self.write_plan()
        sessions, rejected = catalog_sessions(self.root)
        self.assertEqual(sessions, [])
        self.assertIn(DAY, rejected)
        self.assertIn("Unsupported hierarchy", rejected[DAY])

    def test_no_session_folder_returns_empty(self):
        other = self.root / "other"
        self.assertEqual(catalog_sessions(other), ([], {}))


if __name__ == "__main__":
    unittest.main()
