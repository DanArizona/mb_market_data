from __future__ import annotations

import json
import runpy
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from mb_market_data.quote_observation_store import RecordResult


UTC = timezone.utc
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROBE = PROJECT_ROOT / "probes" / "publish_sampling_hierarchy.py"
PROBE_NAMESPACE = runpy.run_path(
    str(PROBE),
    run_name="publish_sampling_hierarchy_probe",
)
load_proposal = PROBE_NAMESPACE["load_proposal"]
parse_publish_at = PROBE_NAMESPACE["parse_publish_at"]
publish_proposal = PROBE_NAMESPACE["publish_proposal"]
validate_publication_schedule = PROBE_NAMESPACE[
    "validate_publication_schedule"
]
wait_until = PROBE_NAMESPACE["wait_until"]


class TestPublishSamplingHierarchyProbe(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "2099-09-10.sqlite3"
        self.proposal_path = self.root / "r0.json"
        self.payload = {
            "session_date": "2099-09-10",
            "revision": 0,
            "effective_at": "2099-09-10T09:30:00-04:00",
            "uni_symbols": ["SPY", "QQQ"],
            "focus_symbols": ["SPY"],
            "hot_symbols": [],
            "source": "controlled-test",
            "reason": "seed r0",
            "metadata": {"operator": "unit-test"},
        }

    def write_payload(self) -> None:
        self.proposal_path.write_text(
            json.dumps(self.payload),
            encoding="utf-8",
        )

    def test_loads_normalizes_and_publishes_v2_proposal(self) -> None:
        self.write_payload()
        proposal = load_proposal(self.proposal_path)
        publication_time = datetime(2099, 9, 10, 12, 0, tzinfo=UTC)

        with patch(
            "mb_market_data.quote_observation_store._utc_now",
            return_value=publication_time,
        ):
            result, stored = publish_proposal(
                self.database_path,
                proposal,
            )

        self.assertEqual(result, RecordResult.INSERTED)
        self.assertEqual(stored.uni_symbols, ("QQQ", "SPY"))
        self.assertEqual(stored.focus_symbols, ("SPY",))
        self.assertEqual(stored.hot_symbols, ())
        self.assertEqual(stored.published_at, publication_time)
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                2,
            )

    def test_identical_retry_reports_already_present(self) -> None:
        self.write_payload()
        proposal = load_proposal(self.proposal_path)
        publication_time = datetime(2099, 9, 10, 12, 0, tzinfo=UTC)
        with patch(
            "mb_market_data.quote_observation_store._utc_now",
            return_value=publication_time,
        ):
            first, _ = publish_proposal(self.database_path, proposal)
            second, _ = publish_proposal(self.database_path, proposal)

        self.assertEqual(first, RecordResult.INSERTED)
        self.assertEqual(second, RecordResult.ALREADY_PRESENT)

    def test_rejects_naive_effective_time_and_unknown_fields(self) -> None:
        self.payload["effective_at"] = "2099-09-10T09:30:00"
        self.write_payload()
        with self.assertRaisesRegex(ValueError, "explicit UTC offset"):
            load_proposal(self.proposal_path)

        self.payload["effective_at"] = "2099-09-10T09:30:00-04:00"
        self.payload["published_at"] = (
            datetime(2099, 9, 10, 12, 0, tzinfo=UTC)
            + timedelta(seconds=1)
        ).isoformat()
        self.write_payload()
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            load_proposal(self.proposal_path)

    def test_scheduled_publication_requires_aware_future_time_before_effective(
        self,
    ) -> None:
        self.write_payload()
        proposal = load_proposal(self.proposal_path)
        observed_at = datetime(2099, 9, 10, 13, 29, tzinfo=UTC)
        publish_at = parse_publish_at("2099-09-10T09:29:30-04:00")

        validate_publication_schedule(
            proposal,
            publish_at,
            observed_at=observed_at,
        )
        with self.assertRaisesRegex(ValueError, "explicit UTC offset"):
            parse_publish_at("2099-09-10T09:29:30")
        with self.assertRaisesRegex(ValueError, "must be in the future"):
            validate_publication_schedule(
                proposal,
                observed_at,
                observed_at=observed_at,
            )
        with self.assertRaisesRegex(ValueError, "must precede"):
            validate_publication_schedule(
                proposal,
                proposal.effective_at,
                observed_at=observed_at,
            )

    def test_wait_until_uses_bounded_final_sleeps(self) -> None:
        target = datetime(2099, 9, 10, 13, 30, tzinfo=UTC)
        readings = iter(
            (
                target - timedelta(seconds=1),
                target - timedelta(milliseconds=200),
                target,
            )
        )
        sleeps: list[float] = []

        wait_until(
            target,
            clock=lambda: next(readings),
            sleep=sleeps.append,
        )

        self.assertEqual(sleeps, [0.25, 0.2])

    def test_september_17_transition_proposals_form_sequential_hierarchy(
        self,
    ) -> None:
        evidence = (
            PROJECT_ROOT
            / "probes"
            / "evidence"
            / "schema_v2_transition_2026-09-17"
        )
        r0 = load_proposal(evidence / "r0.json")
        r1 = load_proposal(evidence / "r1.json")

        self.assertEqual((r0.revision, r1.revision), (0, 1))
        self.assertEqual(len(r0.uni_symbols), 10)
        self.assertEqual(len(r0.focus_symbols), 4)
        self.assertEqual(len(r1.focus_symbols), 5)
        self.assertEqual(r0.hot_symbols, ("SPY",))
        self.assertEqual(r1.hot_symbols, ("MSFT", "SPY"))
        self.assertEqual(r0.uni_symbols, r1.uni_symbols)
        self.assertLess(r0.effective_at, r1.effective_at)


if __name__ == "__main__":
    unittest.main()
