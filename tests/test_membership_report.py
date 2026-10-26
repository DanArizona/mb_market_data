from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from mb_market_data.membership_report import (
    ET, ReportError, build_report, default_database, format_text, normalize_symbols, report_cutoff,
)
from mb_market_data.quote_observation_store import QuoteObservationStore, SamplingChannelRevision
from mb_market_data.sampling_membership import SamplingHierarchyRevision


DAY = date(2026, 10, 26)


def at(clock: str) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{clock}").replace(tzinfo=ET)


class MembershipReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = default_database(self.root, DAY)
        self.store = QuoteObservationStore(self.database, session_date=DAY, schema_version=2)
        self.store.initialize()

    def revision(self, number, clock, *, focus=(), hot=(), uni=("AZTR", "CLRO")):
        with patch("mb_market_data.quote_observation_store._utc_now", return_value=at(clock) - timedelta(minutes=1)):
            self.store.record_membership_revision(SamplingHierarchyRevision(
                session_date=DAY, revision=number, effective_at=at(clock),
                uni_symbols=uni, focus_symbols=focus, hot_symbols=hot,
                source="synthetic-membership-test", reason=f"test r{number}",
            ))

    def report(self, clock="16:00:00", symbols=None):
        return build_report(self.database, DAY, at(clock), symbols or ["AZTR", "CLRO", "SAIQ"])

    def test_entry_exit_reentry_merge_unchanged_membership_and_missing_symbol(self):
        self.revision(0, "09:30:00")
        self.revision(1, "09:30:00", focus=("AZTR",))
        self.revision(2, "10:00:00", focus=("AZTR",), hot=("AZTR",))
        self.revision(3, "11:00:00", focus=("AZTR",))
        self.revision(4, "12:00:00", focus=("AZTR",), hot=("AZTR",))
        report = self.report()
        aztr = report["symbols"]["AZTR"]
        spans = [i for i in aztr["hot"]["intervals"] if i["member"]]
        self.assertEqual([(i["from_et"], i["to_et"]) for i in spans], [
            (at("10:00:00").isoformat(), at("11:00:00").isoformat()),
            (at("12:00:00").isoformat(), at("16:00:00").isoformat()),
        ])
        self.assertEqual(len([i for i in aztr["uni"]["intervals"] if i["member"]]), 1)
        self.assertTrue(aztr["focus"]["at_cutoff"])
        self.assertEqual(aztr["uni"]["intervals"][0]["member"], None)
        self.assertFalse(report["symbols"]["SAIQ"]["uni"]["ever_member"])
        self.assertIsNotNone(report["symbols"]["SAIQ"])
        self.assertIn("earlier membership is unknown", format_text(report))

    def test_cutoff_includes_change_at_exact_boundary_and_excludes_later_changes(self):
        self.revision(0, "09:30:00")
        self.revision(1, "10:00:00", focus=("AZTR",))
        self.revision(2, "11:00:00")
        report = self.report("10:00:00")["symbols"]["AZTR"]["focus"]
        self.assertTrue(report["at_cutoff"])
        self.assertTrue(report["ever_member"])
        self.assertEqual(len(report["revisions"]), 2)
        self.assertFalse(self.report("09:59:59")["symbols"]["AZTR"]["focus"]["at_cutoff"])

    def test_no_revision_before_cutoff_means_null_not_nonmember(self):
        self.revision(0, "09:30:00")
        report = self.report("09:00:00")
        self.assertEqual(report["status"], "NO_DATA")
        self.assertTrue(all(value is None for value in report["symbols"].values()))

    def test_initialized_empty_day_has_null_reports(self):
        self.assertEqual(self.report()["reason"], "no_membership_revisions_up_to_cutoff")

    def test_missing_default_day_is_null_and_does_not_create_directory(self):
        path = default_database(self.root / "missing", DAY)
        report = build_report(path, DAY, at("16:00:00"), ["AZTR"], missing_ok=True)
        self.assertEqual(report["symbols"], {"AZTR": None})
        self.assertFalse(path.parent.exists())

    def test_missing_explicit_database_is_an_error(self):
        with self.assertRaisesRegex(ReportError, "does not exist"):
            build_report(self.root / "typo.sqlite3", DAY, at("16:00:00"), ["AZTR"])

    def test_wrong_day_is_an_error(self):
        with self.assertRaisesRegex(ReportError, "requested"):
            build_report(self.database, date(2026, 10, 27), at("16:00:00").replace(day=27), ["AZTR"])

    def test_unsupported_schema_is_an_error(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA user_version = 99")
        with self.assertRaisesRegex(ReportError, "schema"):
            self.report()

    def test_partial_hierarchy_does_not_misreport_nonmembership(self):
        self.revision(0, "09:30:00")
        with sqlite3.connect(self.database) as connection:
            connection.execute("DELETE FROM sampling_channel_revision WHERE channel = 'hot'")
        with self.assertRaisesRegex(ReportError, "Incomplete"):
            self.report()

    def test_member_count_corruption_is_an_error(self):
        self.revision(0, "09:30:00")
        with sqlite3.connect(self.database) as connection:
            connection.execute("DELETE FROM sampling_channel_member WHERE symbol = 'AZTR'")
        with self.assertRaisesRegex(ReportError, "count mismatch"):
            self.report()

    def test_publication_time_is_exposed_but_membership_starts_at_effective_time(self):
        self.revision(0, "09:30:00")
        channel = self.report()["symbols"]["AZTR"]["uni"]
        self.assertTrue(channel["at_cutoff"])
        self.assertEqual(channel["revisions"][0]["published_at_et"], at("09:29:00").isoformat())
        self.assertEqual(self.report("09:29:30")["status"], "NO_DATA")

    def test_legacy_channel_history_leaves_unrecorded_channels_null(self):
        path = self.root / "legacy.sqlite3"
        store = QuoteObservationStore(path, session_date=DAY, schema_version=1)
        store.initialize()
        store.record_channel_revision(SamplingChannelRevision(
            channel="uni", session_date=DAY, revision=0, effective_at=at("09:30:00"),
            symbols=("AZTR",), source="synthetic-legacy-test",
        ))
        report = build_report(path, DAY, at("16:00:00"), ["AZTR"])
        self.assertTrue(report["symbols"]["AZTR"]["uni"]["at_cutoff"])
        self.assertIsNone(report["symbols"]["AZTR"]["focus"])

    def test_live_wal_read_is_committed_consistent_and_read_only(self):
        self.revision(0, "09:30:00")
        with sqlite3.connect(self.database) as writer:
            writer.execute("PRAGMA journal_mode = WAL")
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("DELETE FROM sampling_channel_member WHERE symbol = 'AZTR'")
            before_db = hashlib.sha256(self.database.read_bytes()).hexdigest()
            before_wal = hashlib.sha256(Path(str(self.database) + "-wal").read_bytes()).hexdigest()
            self.assertTrue(self.report()["symbols"]["AZTR"]["uni"]["at_cutoff"])
            self.assertEqual(before_db, hashlib.sha256(self.database.read_bytes()).hexdigest())
            self.assertEqual(before_wal, hashlib.sha256(Path(str(self.database) + "-wal").read_bytes()).hexdigest())
            writer.rollback()

    def test_quoted_symbols_and_cli_json_exit_without_credentials_or_writes(self):
        self.revision(0, "09:30:00")
        script = Path(__file__).resolve().parents[1] / "probes" / "report_symbol_membership.py"
        before = self.database.read_bytes()
        result = subprocess.run([
            sys.executable, str(script), "aztr CLRO aztr SAIQ", "--date", str(DAY),
            "--database", str(self.database), "--format", "json",
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(list(report["symbols"]), ["AZTR", "CLRO", "SAIQ"])
        self.assertTrue(report["symbols"]["AZTR"]["uni"]["at_cutoff"])
        self.assertEqual(self.database.read_bytes(), before)


class ArgumentTests(unittest.TestCase):
    def test_defaults_use_today_et_not_host_date_and_cap_at_close(self):
        self.assertEqual(report_cutoff(DAY, None, at("13:42:05")), at("13:42:05"))
        self.assertEqual(report_cutoff(DAY, None, at("18:00:00")), at("16:00:00"))
        # 00:30 UTC on Oct27 is still Oct26 ET.
        self.assertEqual(report_cutoff(DAY, None, datetime(2026, 10, 27, 0, 30, tzinfo=timezone.utc)), at("16:00:00"))
        self.assertEqual(report_cutoff(DAY, None, at("10:00:00").replace(day=27)), at("16:00:00"))

    def test_explicit_time_validation_and_dst(self):
        self.assertEqual(report_cutoff(DAY, "11:45", at("18:00:00")), at("11:45:00"))
        for value in ("bad", "25:00", "10:00+01:00"):
            with self.assertRaises(ReportError):
                report_cutoff(DAY, value, at("18:00:00"))
        with self.assertRaises(ReportError):
            report_cutoff(date(2026, 11, 1), "01:30", at("18:00:00"))

    def test_symbols_preserve_order_deduplicate_and_keep_punctuation(self):
        self.assertEqual(normalize_symbols([" aztr  CLRO aztr ", "BRK/B"]), ["AZTR", "CLRO", "BRK/B"])
        with self.assertRaises(ReportError):
            normalize_symbols(["  "])


if __name__ == "__main__":
    unittest.main()
