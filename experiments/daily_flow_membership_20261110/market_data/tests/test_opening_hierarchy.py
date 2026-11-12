from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from mb_market_data.daily_universe import (
    decide_daily_universe,
    write_daily_universe_artifacts,
)
from mb_market_data.opening_hierarchy import (
    build_opening_hierarchy,
    load_opening_proposal,
    write_opening_proposal,
)
from mb_market_data.quote_observation_store import (
    HIERARCHY_SCHEMA_VERSION,
    QuoteObservationStore,
    RecordResult,
)
from mb_market_data.sampling_membership import (
    RevisionTransition,
    SamplingHierarchyRevision,
    assess_revision_candidate,
)


class TestOpeningHierarchy(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source_path = self.root / "normalized_all.csv"
        self.market_path = self.root / "market_data_snapshot.csv"
        self.universe_dir = self.root / "universe"
        sources = [
            {
                "symbol": "DAIC",
                "security_name": "CID HoldCo",
                "source": "nasdaqlisted",
                "listing_exchange_code": "Q",
                "listing_exchange_name": "NASDAQ",
                "market_category": "G",
                "test_issue": "N",
                "financial_status": "H",
                "etf": "N",
            },
            {
                "symbol": "ABCD",
                "security_name": "ABCD Corporation",
                "source": "otherlisted",
                "listing_exchange_code": "N",
                "listing_exchange_name": "NYSE",
                "market_category": "",
                "test_issue": "N",
                "financial_status": "",
                "etf": "N",
            },
        ]
        markets = [
            {
                "symbol": symbol,
                "acquisition_status": "quote",
                "close_price": "4",
                "total_volume": "10000",
                "shares_outstanding": "2000000",
                "regular_market_session_match": "true",
            }
            for symbol in ("DAIC", "ABCD")
        ]
        self._write_csv(self.source_path, sources)
        self._write_csv(self.market_path, markets)
        decisions = decide_daily_universe(sources, markets)
        write_daily_universe_artifacts(
            output_dir=self.universe_dir,
            decisions=decisions,
            symbol_directory_path=self.source_path,
            market_data_path=self.market_path,
            session_date=date(2026, 9, 18),
            target_date=date(2026, 9, 21),
            generated_at_utc=datetime(
                2026, 9, 19, 1, 0, tzinfo=timezone.utc
            ),
        )

    @staticmethod
    def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
        with path.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def test_builds_opening_r0_with_empty_focus_and_hot(self) -> None:
        revision = build_opening_hierarchy(self.universe_dir)

        self.assertEqual(revision.session_date, date(2026, 9, 21))
        self.assertEqual(revision.revision, 0)
        self.assertEqual(
            revision.effective_at.isoformat(),
            "2026-09-21T09:30:00-04:00",
        )
        self.assertEqual(revision.uni_symbols, ("ABCD", "DAIC"))
        self.assertEqual(revision.focus_symbols, ())
        self.assertEqual(revision.hot_symbols, ())
        self.assertEqual(
            revision.metadata["opening_focus_policy"],
            "empty-awaiting-ov-base-set",
        )

    def test_written_proposal_publishes_to_schema_v2(self) -> None:
        revision = build_opening_hierarchy(self.universe_dir)
        proposal_path = write_opening_proposal(
            self.root / "r0.json",
            revision,
        )
        payload = json.loads(proposal_path.read_text(encoding="utf-8"))
        database = self.root / "2026-09-21.sqlite3"
        store = QuoteObservationStore(
            database,
            session_date=revision.session_date,
            schema_version=HIERARCHY_SCHEMA_VERSION,
        )
        store.initialize()
        with patch(
            "mb_market_data.quote_observation_store._utc_now",
            return_value=datetime(2026, 9, 21, 13, 29, tzinfo=timezone.utc),
        ):
            result = store.record_membership_revision(revision)

        self.assertEqual(payload["revision"], 0)
        self.assertEqual(payload["focus_symbols"], [])
        self.assertEqual(result, RecordResult.INSERTED)
        stored = store.membership_revisions_in_effective_order()[0]
        self.assertEqual(stored.content_sha256, revision.content_sha256)

    def test_loads_written_opening_proposal_exactly(self) -> None:
        revision = build_opening_hierarchy(self.universe_dir)
        proposal_path = write_opening_proposal(
            self.root / "r0.json",
            revision,
        )

        loaded = load_opening_proposal(proposal_path)

        self.assertEqual(loaded.content_sha256, revision.content_sha256)

    def test_loader_rejects_unexpected_fields(self) -> None:
        revision = build_opening_hierarchy(self.universe_dir)
        proposal_path = write_opening_proposal(
            self.root / "r0.json",
            revision,
        )
        payload = json.loads(proposal_path.read_text(encoding="utf-8"))
        payload["unreviewed"] = True
        proposal_path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            load_opening_proposal(proposal_path)

    def test_rejects_tampered_uni_symbols(self) -> None:
        with (self.universe_dir / "uni_symbols.csv").open(
            "a", encoding="utf-8"
        ) as file:
            file.write("EXTRA\n")

        with self.assertRaisesRegex(ValueError, "SHA-256"):
            build_opening_hierarchy(self.universe_dir)

    def test_ov_base_set_can_become_r1_at_the_open(self) -> None:
        opening = build_opening_hierarchy(self.universe_dir)
        ov_revision = SamplingHierarchyRevision(
            session_date=opening.session_date,
            revision=1,
            effective_at=opening.effective_at,
            uni_symbols=opening.uni_symbols,
            focus_symbols=("DAIC",),
            hot_symbols=(),
            source="overnight-volume-producer",
            reason="opening OV BASE_SET",
            metadata={"producer_revision": 1},
        )

        self.assertEqual(
            assess_revision_candidate(opening, ov_revision),
            RevisionTransition.NEXT,
        )


if __name__ == "__main__":
    unittest.main()
