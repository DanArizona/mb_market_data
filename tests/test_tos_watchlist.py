from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mb_market_data.tos_watchlist import (
    OVDecisionStatus,
    classify_ov_decision,
    read_tos_watchlist,
)


class TestClassifyOVDecision(unittest.TestCase):

    def test_positive_integer(self) -> None:
        value, status = classify_ov_decision(
            "12345"
        )

        self.assertEqual(value, 12345)
        self.assertEqual(
            status,
            OVDecisionStatus.NUMERIC,
        )

    def test_integral_decimal(self) -> None:
        value, status = classify_ov_decision(
            "607650.0"
        )

        self.assertEqual(value, 607650)
        self.assertEqual(
            status,
            OVDecisionStatus.NUMERIC,
        )

    def test_comma_separated_integer(self) -> None:
        value, status = classify_ov_decision(
            "607,650"
        )

        self.assertEqual(value, 607650)
        self.assertEqual(
            status,
            OVDecisionStatus.NUMERIC,
        )

    def test_zero_is_valid(self) -> None:
        value, status = classify_ov_decision(
            "0.0"
        )

        self.assertEqual(value, 0)
        self.assertEqual(
            status,
            OVDecisionStatus.ZERO,
        )

    def test_loading(self) -> None:
        value, status = classify_ov_decision(
            "loading"
        )

        self.assertIsNone(value)
        self.assertEqual(
            status,
            OVDecisionStatus.LOADING,
        )

    def test_nan(self) -> None:
        value, status = classify_ov_decision(
            "NaN"
        )

        self.assertIsNone(value)
        self.assertEqual(
            status,
            OVDecisionStatus.NAN,
        )

    def test_blank(self) -> None:
        value, status = classify_ov_decision(
            ""
        )

        self.assertIsNone(value)
        self.assertEqual(
            status,
            OVDecisionStatus.BLANK,
        )

    def test_empty_marker(self) -> None:
        value, status = classify_ov_decision(
            "<empty>"
        )

        self.assertIsNone(value)
        self.assertEqual(
            status,
            OVDecisionStatus.BLANK,
        )

    def test_subscription_limit(self) -> None:
        value, status = classify_ov_decision(
            "custom expression subscription limit exceeded"
        )

        self.assertIsNone(value)
        self.assertEqual(
            status,
            OVDecisionStatus.SUBSCRIPTION_LIMIT,
        )

    def test_fractional_volume_is_invalid(self) -> None:
        value, status = classify_ov_decision(
            "12.5"
        )

        self.assertIsNone(value)
        self.assertEqual(
            status,
            OVDecisionStatus.INVALID,
        )

    def test_negative_volume_is_invalid(self) -> None:
        value, status = classify_ov_decision(
            "-1"
        )

        self.assertIsNone(value)
        self.assertEqual(
            status,
            OVDecisionStatus.INVALID,
        )

    def test_unrecognized_text_is_invalid(self) -> None:
        value, status = classify_ov_decision(
            "something strange"
        )

        self.assertIsNone(value)
        self.assertEqual(
            status,
            OVDecisionStatus.INVALID,
        )


class TestReadTosWatchlist(unittest.TestCase):

    def make_csv(
        self,
        directory: Path,
        contents: str,
    ) -> Path:
        path = directory / "watchlist.csv"

        path.write_text(
            contents,
            encoding="utf-8",
        )

        return path

    def test_reads_tos_preamble_and_rows(self) -> None:
        contents = (
            "Watchlist Scanner\n"
            "\n"
            "Results\n"
            "Symbol,Market Cap,OV_DECISION,Volume,Last\n"
            "AAPL,1 T,12345,100000,200.00\n"
            "MSFT,2 T,0.0,200000,300.00\n"
            "SPY,,607650.0,300000,600.00\n"
            "LOAD,,loading,0,1.00\n"
            "NAN1,,NaN,0,1.00\n"
            "BLANK,,,0,1.00\n"
            "LIMIT,,custom expression subscription limit exceeded,0,1.00\n"
            "BAD,,12.5,0,1.00\n"
        )

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)

            path = self.make_csv(
                directory,
                contents,
            )

            watchlist = read_tos_watchlist(
                path
            )

        self.assertEqual(
            watchlist.header_line_number,
            4,
        )

        self.assertEqual(
            len(watchlist.rows),
            8,
        )

        self.assertEqual(
            len(watchlist.usable_rows),
            3,
        )

        self.assertEqual(
            len(watchlist.unavailable_rows),
            5,
        )

        by_symbol = {
            row.symbol: row
            for row in watchlist.rows
        }

        self.assertEqual(
            by_symbol["AAPL"].ov_decision,
            12345,
        )

        self.assertEqual(
            by_symbol["MSFT"].ov_decision,
            0,
        )

        self.assertEqual(
            by_symbol["SPY"].ov_decision,
            607650,
        )

        self.assertEqual(
            by_symbol["LOAD"].ov_decision_status,
            OVDecisionStatus.LOADING,
        )

        self.assertEqual(
            by_symbol["NAN1"].ov_decision_status,
            OVDecisionStatus.NAN,
        )

        self.assertEqual(
            by_symbol["BLANK"].ov_decision_status,
            OVDecisionStatus.BLANK,
        )

        self.assertEqual(
            by_symbol["LIMIT"].ov_decision_status,
            OVDecisionStatus.SUBSCRIPTION_LIMIT,
        )

        self.assertEqual(
            by_symbol["BAD"].ov_decision_status,
            OVDecisionStatus.INVALID,
        )

    def test_missing_ov_decision_column_raises(self) -> None:
        contents = (
            "Watchlist Scanner\n"
            "\n"
            "Results\n"
            "Symbol,Volume,Last\n"
            "SPY,1000,600\n"
        )

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)

            path = self.make_csv(
                directory,
                contents,
            )

            with self.assertRaisesRegex(
                ValueError,
                "OV_DECISION",
            ):
                read_tos_watchlist(path)

    def test_missing_symbol_header_raises(self) -> None:
        contents = (
            "Watchlist Scanner\n"
            "\n"
            "Results\n"
            "Ticker,OV_DECISION\n"
            "SPY,12345\n"
        )

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)

            path = self.make_csv(
                directory,
                contents,
            )

            with self.assertRaisesRegex(
                ValueError,
                "Symbol header",
            ):
                read_tos_watchlist(path)


if __name__ == "__main__":
    unittest.main()
