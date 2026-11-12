from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mb_market_data.nasdaq_symbol_directory import (
    build_symbol_directory_snapshot,
    write_symbol_directory_artifacts,
)


NASDAQ_RAW = b"""Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
DAIC|CID HoldCo, Inc. - Common Stock|G|N|H|100|N|N
QQQ|Invesco QQQ Trust|Q|N|N|100|Y|N
TEST|Nasdaq Test Issue|S|Y|N|100|N|N
File Creation Time: 0918202618:01|||||||
"""

OTHER_RAW = b"""ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
XYZ|XYZ Corporation|N|XYZ|N|100|N|XYZ
File Creation Time: 0918202618:01|||||||
"""


class TestNasdaqSymbolDirectory(unittest.TestCase):
    def test_normalizes_both_sources_and_filters_candidates(self) -> None:
        snapshot = build_symbol_directory_snapshot(NASDAQ_RAW, OTHER_RAW)

        self.assertEqual(
            [row["symbol"] for row in snapshot.normalized_rows],
            ["DAIC", "QQQ", "TEST", "XYZ"],
        )
        self.assertEqual(
            [row["symbol"] for row in snapshot.candidate_rows],
            ["DAIC", "XYZ"],
        )
        daic = snapshot.normalized_rows[0]
        self.assertEqual(daic["financial_status"], "H")
        self.assertEqual(daic["listing_exchange_name"], "NASDAQ")
        xyz = snapshot.normalized_rows[-1]
        self.assertEqual(xyz["listing_exchange_name"], "NYSE")
        self.assertEqual(snapshot.nasdaq_file_creation_time, "0918202618:01")

    def test_rejects_duplicate_symbols_across_sources(self) -> None:
        duplicate_other = OTHER_RAW.replace(b"XYZ|", b"DAIC|")

        with self.assertRaisesRegex(ValueError, "duplicate.*DAIC"):
            build_symbol_directory_snapshot(NASDAQ_RAW, duplicate_other)

    def test_writes_immutable_hashed_artifacts(self) -> None:
        snapshot = build_symbol_directory_snapshot(NASDAQ_RAW, OTHER_RAW)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "directory"
            paths = write_symbol_directory_artifacts(
                output_dir=output,
                snapshot=snapshot,
                retrieved_at_et=datetime(
                    2026,
                    9,
                    18,
                    20,
                    39,
                    tzinfo=ZoneInfo("America/New_York"),
                ),
            )
            with paths["candidates"].open(encoding="utf-8") as file:
                candidates = list(csv.DictReader(file))
            manifest = json.loads(
                paths["manifest"].read_text(encoding="utf-8")
            )

            self.assertEqual(
                [row["symbol"] for row in candidates], ["DAIC", "XYZ"]
            )
            self.assertEqual(manifest["counts"]["combined"], 4)
            self.assertEqual(manifest["counts"]["preliminary_candidates"], 2)
            self.assertEqual(
                len(manifest["artifacts"]["normalized_all"]["sha256"]),
                64,
            )
            with self.assertRaises(FileExistsError):
                write_symbol_directory_artifacts(
                    output_dir=output,
                    snapshot=snapshot,
                    retrieved_at_et=datetime.now(
                        ZoneInfo("America/New_York")
                    ),
                )


if __name__ == "__main__":
    unittest.main()
