"""Native membership journals with saved, published-hash-bound decision ledgers."""
from __future__ import annotations

from contextlib import closing
import csv
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mb_market_data.membership_report import ET, ReportError, build_report, default_database, format_text
from mb_market_data.quote_observation_store import QuoteObservationStore, SamplingChannelRevision
from mb_market_data.sampling_membership import SamplingHierarchyRevision

DAY = date(2026, 10, 26)


def at(clock):
    return datetime.fromisoformat(f"{DAY}T{clock}").replace(tzinfo=ET)


class MembershipReasonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = default_database(self.root, DAY)
        self.store = QuoteObservationStore(self.database, session_date=DAY, schema_version=2)
        self.store.initialize()

    def ledger(self, *, channel="uni", root=None, rows=None):
        path = (root or self.root / "output" / "daily_universe_production" / "original-source" / "universe")
        path.mkdir(parents=True, exist_ok=True)
        path /= "decision_ledger.csv" if channel == "uni" else "focus_decision_ledger.csv"
        rows = rows or [
            {"symbol": "CLRO", "decision": "include", "primary_reason": "included",
             "reason_codes": "included", "calculated_market_cap": "8000000", "total_volume": "25000"},
            {"symbol": "AZTR", "decision": "reject", "primary_reason": "market_cap_above_max",
             "reason_codes": "market_cap_above_max;volume_below_min", "calculated_market_cap": "42000000",
             "total_volume": "9000"},
        ]
        fields = list(dict.fromkeys(key for row in rows for key in row))
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader(); writer.writerows(rows)
        metadata = {"decision_ledger_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        if channel == "uni":
            metadata.update(universe_source_session_date="2026-10-22", universe_manifest_sha256="a" * 64)
        else:
            metadata.update(focus_symbols_sha256="b" * 64, requested_limit=40)
        return path, metadata

    def revision(self, revision=0, clock="09:30:00", *, uni=("CLRO",), focus=(), hot=(), metadata=None, reason=None):
        with patch("mb_market_data.quote_observation_store._utc_now", return_value=at(clock) - timedelta(minutes=1)):
            self.store.record_membership_revision(SamplingHierarchyRevision(
                session_date=DAY, revision=revision, effective_at=at(clock), uni_symbols=uni,
                focus_symbols=focus, hot_symbols=hot, source="synthetic report revision",
                reason=reason, metadata=metadata or {},
            ))

    def report(self, *, symbols=("AZTR", "CLRO"), cutoff="16:00:00", roots=None):
        return build_report(self.database, DAY, at(cutoff), list(symbols),
                            market_data_root=self.root, evidence_roots=roots)

    def test_never_uni_prints_verified_rejection_and_suppresses_both_children(self):
        path, metadata = self.ledger()
        self.revision(metadata=metadata)
        report = self.report()
        text = format_text(report).split("\nAZTR\n")[1].split("\nCLRO\n")[0]
        self.assertIn("UNI: never a member | market cap above max; volume below min", text)
        self.assertIn("source session=2026-10-22", text)
        self.assertNotIn("FOCUS:", text); self.assertNotIn("HOT:", text)
        self.assertEqual(report["symbols"]["AZTR"]["uni"]["never_reason"]["ledger"], str(path))
        self.assertFalse(report["symbols"]["AZTR"]["focus"]["ever_member"])

    def test_entry_exit_reentry_prints_actual_changes_with_reasons_and_coalesces_unchanged(self):
        self.revision(reason="opening")
        self.revision(1, "10:00:00", focus=("CLRO",), reason="manual promotion")
        self.revision(2, "10:30:00", focus=("CLRO",), reason="unrelated change")
        self.revision(3, "11:00:00", reason="manual withdrawal")
        self.revision(4, "12:00:00", focus=("CLRO",), reason="second promotion")
        report = self.report(symbols=("CLRO",))
        changes = report["symbols"]["CLRO"]["focus"]["changes"]
        self.assertEqual([change["action"] for change in changes], ["not a member", "joined", "left", "joined"])
        text = format_text(report)
        self.assertIn("10:00:00 joined | revision reason: manual promotion", text)
        self.assertIn("11:00:00 left | revision reason: manual withdrawal", text)
        self.assertIn("12:00:00 joined | revision reason: second promotion", text)
        self.assertNotIn("unrelated change", text)

    def test_never_focus_suppresses_hot_but_keeps_uni(self):
        self.revision()
        text = format_text(self.report(symbols=("CLRO",)))
        self.assertIn("UNI: at cutoff: YES", text)
        self.assertIn("FOCUS: never a member", text)
        self.assertNotIn("HOT:", text)

    def test_no_data_retains_null_instead_of_fabricating_a_reason(self):
        report = self.report()
        self.assertEqual(report["status"], "NO_DATA")
        self.assertIn("AZTR: NULL", format_text(report))
        self.assertNotIn("never a member", format_text(report))

    def test_focus_rank_and_limit_come_from_the_exact_published_focus_ledger(self):
        self.revision(uni=("CLRO", "AZTR"))
        path, metadata = self.ledger(channel="focus", root=self.root / "output" / "ov_focus_production" / "run", rows=[
            {"symbol": "CLRO", "decision": "include", "primary_reason": "included", "ov_decision": "30000", "eligible_rank": "1"},
            {"symbol": "AZTR", "decision": "exclude", "primary_reason": "below_selection_limit", "ov_decision": "100", "eligible_rank": "41"},
        ])
        self.revision(1, uni=("CLRO", "AZTR"), focus=("CLRO",), metadata=metadata)
        report = self.report()
        why = report["symbols"]["AZTR"]["focus"]["never_reason"]
        self.assertEqual(why["ledger"], str(path))
        self.assertIn("eligible rank=41", why["message"])
        self.assertIn("selection limit=40", why["message"])

    def test_later_focus_metadata_does_not_replace_universe_rejection_evidence(self):
        _, metadata = self.ledger()
        self.revision(metadata=metadata)
        _, focus_metadata = self.ledger(channel="focus", root=self.root / "output" / "ov_focus_production", rows=[
            {"symbol": "CLRO", "decision": "include", "primary_reason": "included"},
        ])
        self.revision(1, focus=("CLRO",), metadata=focus_metadata)
        why = self.report()["symbols"]["AZTR"]["uni"]["never_reason"]
        self.assertEqual(why["codes"], ["market_cap_above_max", "volume_below_min"])

    def test_preserved_nested_carry_forward_metadata_finds_original_ledger(self):
        _, metadata = self.ledger()
        self.revision(metadata={"carry_forward": {"original_metadata": metadata}, "source_date": "2026-10-23"})
        why = self.report()["symbols"]["AZTR"]["uni"]["never_reason"]
        self.assertEqual(why["source_session_date"], "2026-10-22")
        self.assertEqual(why["kind"], "selector")

    def test_a_newer_different_ledger_is_not_substituted_for_the_published_source(self):
        original, metadata = self.ledger()
        self.ledger(root=self.root / "output" / "daily_universe_production" / "tomorrow" / "universe", rows=[
            {"symbol": "AZTR", "decision": "include", "primary_reason": "included"},
        ])
        self.revision(metadata=metadata)
        self.assertEqual(self.report()["symbols"]["AZTR"]["uni"]["never_reason"]["ledger"], str(original))

    def test_missing_ledger_leaves_membership_valid_and_reason_explicitly_unavailable(self):
        path, metadata = self.ledger(); path.unlink()
        self.revision(metadata=metadata)
        report = self.report()
        self.assertEqual(report["status"], "OK")
        self.assertFalse(report["symbols"]["AZTR"]["uni"]["ever_member"])
        self.assertIn("reason unavailable", report["symbols"]["AZTR"]["uni"]["never_reason"]["message"])

    def test_altered_ledger_is_not_used_even_when_its_name_and_date_look_right(self):
        path, metadata = self.ledger(); self.revision(metadata=metadata)
        path.write_text(path.read_text().replace("42000000", "9000000"))
        why = self.report()["symbols"]["AZTR"]["uni"]["never_reason"]
        self.assertEqual(why["kind"], "unavailable")
        self.assertNotIn("9000000", why["message"])

    def test_matching_hash_cannot_override_a_different_published_roster(self):
        _, metadata = self.ledger()
        self.revision(uni=("OTHER",), metadata=metadata)
        why = self.report()["symbols"]["AZTR"]["uni"]["never_reason"]
        self.assertIn("inclusion set differs", why["message"])

    def test_unbound_ledger_is_not_used_to_invent_a_reason(self):
        self.ledger(); self.revision()
        why = self.report()["symbols"]["AZTR"]["uni"]["never_reason"]
        self.assertEqual(why["kind"], "unavailable")

    def test_unreadable_evidence_folder_does_not_erase_valid_membership(self):
        _, metadata = self.ledger(); self.revision(metadata=metadata)
        with patch.object(Path, "rglob", side_effect=PermissionError("synthetic denied folder")):
            report = self.report()
        self.assertEqual(report["status"], "OK")
        self.assertFalse(report["symbols"]["AZTR"]["uni"]["ever_member"])
        self.assertEqual(report["symbols"]["AZTR"]["uni"]["never_reason"]["kind"], "unavailable")

    def test_symbol_absent_from_verified_ledger_has_no_invented_exclusion(self):
        _, metadata = self.ledger(); self.revision(metadata=metadata)
        why = self.report(symbols=("UNLISTED",))["symbols"]["UNLISTED"]["uni"]["never_reason"]
        self.assertEqual(why["kind"], "unavailable")
        self.assertIn("no decision for this symbol", why["message"])

    def test_extra_evidence_root_can_find_a_preserved_archive(self):
        archive = self.root / "saved archive"
        _, metadata = self.ledger(root=archive)
        self.revision(metadata=metadata)
        self.assertEqual(self.report(roots=[archive])["symbols"]["AZTR"]["uni"]["never_reason"]["kind"], "selector")

    def test_same_instant_membership_states_are_not_hidden_as_never_members(self):
        self.revision(uni=("CLRO", "AZTR"))
        self.revision(1, uni=("CLRO",))
        report = self.report(cutoff="09:30:00")
        self.assertTrue(report["symbols"]["AZTR"]["uni"]["ever_member"])
        text = format_text(report)
        self.assertIn("09:30:00 joined", text); self.assertIn("09:30:00 left", text)

    def test_cutoff_includes_exact_entry_and_excludes_future_exits(self):
        self.revision(); self.revision(1, "10:00:00", focus=("CLRO",))
        self.revision(2, "11:00:00")
        report = self.report(symbols=("CLRO",), cutoff="10:00:00")
        self.assertTrue(report["symbols"]["CLRO"]["focus"]["at_cutoff"])
        self.assertNotIn("11:00", format_text(report))

    def test_journal_and_evidence_bytes_are_unchanged(self):
        path, metadata = self.ledger(); self.revision(metadata=metadata)
        before = (self.database.read_bytes(), path.read_bytes())
        self.report()
        self.assertEqual(before, (self.database.read_bytes(), path.read_bytes()))

    def test_legacy_channels_are_not_suppressed_using_an_unproven_hierarchy(self):
        path = self.root / "legacy.sqlite3"
        store = QuoteObservationStore(path, session_date=DAY, schema_version=1); store.initialize()
        for name, members in (("uni", ("OTHER",)), ("focus", ("AZTR",))):
            store.record_channel_revision(SamplingChannelRevision(
                channel=name, session_date=DAY, revision=0, effective_at=at("09:30:00"), symbols=members,
                source="synthetic legacy",
            ))
        report = build_report(path, DAY, at("16:00:00"), ["AZTR"])
        self.assertIn("FOCUS: at cutoff: YES", format_text(report))
        self.assertIn("HOT: NULL", format_text(report))

    def test_hierarchy_metadata_disagreement_is_refused(self):
        self.revision()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE sampling_channel_revision SET metadata_json = ? WHERE channel = 'uni'", ('{"tampered": true}',))
        with self.assertRaisesRegex(ReportError, "provenance mismatch"):
            self.report()

    def test_duplicate_symbol_rows_in_a_matching_ledger_are_reported_unavailable(self):
        _, metadata = self.ledger(rows=[
            {"symbol": "CLRO", "decision": "include", "primary_reason": "included"},
            {"symbol": "CLRO", "decision": "include", "primary_reason": "included"},
        ])
        self.revision(metadata=metadata)
        self.assertIn("duplicate symbols", self.report()["symbols"]["AZTR"]["uni"]["never_reason"]["message"])

    def test_cli_handles_the_existing_symbols_string_and_returns_changes_json(self):
        _, metadata = self.ledger(); self.revision(metadata=metadata)
        script = Path(__file__).resolve().parents[1] / "probes" / "report_symbol_membership.py"
        result = subprocess.run([sys.executable, str(script), "AZTR CLRO AZTR", "--date", str(DAY),
                                 "--market-data-root", str(self.root), "--format", "json"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(list(report["symbols"]), ["AZTR", "CLRO"])
        self.assertEqual(report["contract"], "daily-symbol-membership-report-v2")
        self.assertEqual(report["symbols"]["AZTR"]["uni"]["never_reason"]["kind"], "selector")


if __name__ == "__main__":
    unittest.main()
