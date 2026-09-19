from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from mb_market_data.daily_universe import (
    UniverseFilterConfig,
    decide_daily_universe,
    write_daily_universe_artifacts,
)
from mb_market_data.tos_watchlist import read_tos_watchlist


def source(symbol: str, **changes: str) -> dict[str, str]:
    row = {
        "symbol": symbol,
        "security_name": f"{symbol} Incorporated",
        "source": "nasdaqlisted",
        "listing_exchange_code": "Q",
        "listing_exchange_name": "NASDAQ",
        "market_category": "S",
        "test_issue": "N",
        "financial_status": "N",
        "etf": "N",
    }
    row.update(changes)
    return row


def market(symbol: str, **changes: str) -> dict[str, str]:
    row = {
        "symbol": symbol,
        "acquisition_status": "quote",
        "acquisition_detail": "",
        "close_price": "1.00",
        "total_volume": "10000",
        "shares_outstanding": "4000000",
        "direct_market_cap": "1",
    }
    row.update(changes)
    return row


class TestDecideDailyUniverse(unittest.TestCase):

    def test_includes_values_on_all_inclusive_boundaries(self) -> None:
        decisions = decide_daily_universe(
            [source("LOW"), source("HIGH")],
            [
                market("LOW"),
                market(
                    "HIGH",
                    close_price="0.10",
                    total_volume="10,000",
                    shares_outstanding="400,000,000",
                ),
            ],
        )

        self.assertEqual(
            [decision.symbol for decision in decisions],
            ["HIGH", "LOW"],
        )
        self.assertTrue(all(decision.included for decision in decisions))
        self.assertTrue(
            all(decision.reason_codes == "included" for decision in decisions)
        )

    def test_records_every_numeric_rejection_reason(self) -> None:
        decision = decide_daily_universe(
            [source("FAIL")],
            [
                market(
                    "FAIL",
                    close_price="0.09",
                    total_volume="9999",
                    shares_outstanding="500000000",
                )
            ],
        )[0]

        self.assertEqual(decision.decision, "reject")
        self.assertEqual(decision.primary_reason, "volume_below_min")
        self.assertEqual(
            decision.reason_codes,
            "volume_below_min;close_below_min;market_cap_above_max",
        )

    def test_etf_and_test_issue_are_explained_without_market_row(self) -> None:
        decisions = decide_daily_universe(
            [source("ETF1", etf="Y"), source("TEST", test_issue="Y")],
            [],
        )

        by_symbol = {decision.symbol: decision for decision in decisions}
        self.assertEqual(by_symbol["ETF1"].reason_codes, "etf")
        self.assertEqual(by_symbol["TEST"].reason_codes, "test_issue")

    def test_missing_candidate_market_data_is_explicit(self) -> None:
        decision = decide_daily_universe([source("DAIC")], [])[0]

        self.assertEqual(decision.primary_reason, "missing_market_data")

    def test_acquisition_and_missing_shares_are_explicit(self) -> None:
        decision = decide_daily_universe(
            [source("BAD")],
            [
                market(
                    "BAD",
                    acquisition_status="invalid",
                    shares_outstanding="",
                    direct_market_cap="",
                )
            ],
        )[0]

        self.assertEqual(
            decision.reason_codes,
            "acquisition_invalid;missing_shares_outstanding",
        )

    def test_missing_required_status_and_values_are_explicit(self) -> None:
        decision = decide_daily_universe(
            [source("EMPTY")],
            [
                market(
                    "EMPTY",
                    acquisition_status="",
                    total_volume="",
                    close_price="",
                    shares_outstanding="",
                    direct_market_cap="",
                )
            ],
        )[0]

        self.assertEqual(
            decision.reason_codes,
            (
                "missing_acquisition_status;missing_volume;missing_close;"
                "missing_shares_outstanding"
            ),
        )

    def test_daic_uses_local_cap_not_inconsistent_direct_cap(self) -> None:
        decision = decide_daily_universe(
            [source("DAIC")],
            [
                market(
                    "DAIC",
                    close_price="3.55",
                    total_volume="4099267",
                    shares_outstanding="1210383",
                    direct_market_cap="2432869",
                    regular_market_session_match="true",
                    regular_market_trade_time_et=(
                        "2026-09-18T16:00:00.596000-04:00"
                    ),
                )
            ],
        )[0]

        self.assertTrue(decision.included)
        self.assertEqual(decision.calculated_market_cap, "4296859.65")
        self.assertEqual(decision.direct_market_cap, "2432869")

    def test_rejects_stale_regular_market_trade(self) -> None:
        decision = decide_daily_universe(
            [source("STALE")],
            [market("STALE", regular_market_session_match="false")],
        )[0]

        self.assertEqual(
            decision.primary_reason,
            "regular_trade_not_in_session",
        )

    def test_rejects_duplicate_and_unknown_symbols(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate symbol 'DUP'"):
            decide_daily_universe([source("DUP"), source("dup")], [])

        with self.assertRaisesRegex(ValueError, "absent from the symbol directory"):
            decide_daily_universe([source("KNOWN")], [market("UNKNOWN")])

    def test_validates_filter_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "maximum_market_cap"):
            UniverseFilterConfig(
                minimum_market_cap=Decimal("10"),
                maximum_market_cap=Decimal("9"),
            )


class TestWriteDailyUniverseArtifacts(unittest.TestCase):

    def write_input(self, path: Path, rows: list[dict[str, str]]) -> None:
        with path.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def test_writes_pollable_watchlist_ledger_and_hashed_manifest(self) -> None:
        source_rows = [source("YES"), source("NO")]
        market_rows = [market("YES"), market("NO", total_volume="1")]
        decisions = decide_daily_universe(source_rows, market_rows)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "normalized_all.csv"
            market_path = root / "enriched.csv"
            self.write_input(source_path, source_rows)
            self.write_input(market_path, market_rows)

            paths = write_daily_universe_artifacts(
                output_dir=root / "result",
                decisions=decisions,
                symbol_directory_path=source_path,
                market_data_path=market_path,
                session_date=date(2026, 9, 17),
                target_date=date(2026, 9, 18),
                generated_at_utc=datetime(2026, 9, 17, 21, tzinfo=timezone.utc),
            )

            watchlist = read_tos_watchlist(paths["uni_watchlist"])
            manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
            ledger = list(
                csv.DictReader(paths["decision_ledger"].open(encoding="utf-8"))
            )

        self.assertEqual([row.symbol for row in watchlist.rows], ["YES"])
        self.assertEqual([row["symbol"] for row in ledger], ["NO", "YES"])
        self.assertEqual(manifest["counts"], {
            "included": 1,
            "rejected": 1,
            "source_symbols": 2,
        })
        self.assertEqual(manifest["reason_counts"], {
            "included": 1,
            "volume_below_min": 1,
        })
        self.assertEqual(
            len(manifest["inputs"]["symbol_directory"]["sha256"]),
            64,
        )
        self.assertEqual(
            len(manifest["artifacts"]["decision_ledger"]["sha256"]),
            64,
        )

    def test_refuses_to_overwrite_daily_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "source.csv"
            market_path = root / "market.csv"
            self.write_input(source_path, [source("ONE")])
            self.write_input(market_path, [market("ONE")])
            output = root / "result"
            output.mkdir()

            with self.assertRaises(FileExistsError):
                write_daily_universe_artifacts(
                    output_dir=output,
                    decisions=decide_daily_universe(
                        [source("ONE")], [market("ONE")]
                    ),
                    symbol_directory_path=source_path,
                    market_data_path=market_path,
                    session_date=date(2026, 9, 17),
                    target_date=date(2026, 9, 18),
                )

    def test_command_builds_artifacts_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "normalized_all.csv"
            market_path = root / "enriched.csv"
            output_path = root / "daily"
            self.write_input(source_path, [source("YES"), source("NO")])
            self.write_input(
                market_path,
                [market("YES"), market("NO", close_price="0.01")],
            )

            repository_root = Path(__file__).resolve().parents[1]
            environment = dict(os.environ)
            existing_python_path = environment.get("PYTHONPATH", "")
            environment["PYTHONPATH"] = os.pathsep.join(
                value
                for value in [
                    str(repository_root / "src"),
                    existing_python_path,
                ]
                if value
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(repository_root / "probes" / "build_daily_universe.py"),
                    "--symbol-directory",
                    str(source_path),
                    "--market-data",
                    str(market_path),
                    "--session-date",
                    "2026-09-17",
                    "--target-date",
                    "2026-09-18",
                    "--output-dir",
                    str(output_path),
                ],
                cwd=repository_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            manifest = json.loads(
                (output_path / "manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Daily universe build: PASS", result.stdout)
        self.assertEqual(manifest["counts"]["included"], 1)


if __name__ == "__main__":
    unittest.main()
