from __future__ import annotations

import csv
import runpy
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROBE = PROJECT_ROOT / "probes" / "probe_daily_ohlcv.py"
NAMESPACE = runpy.run_path(str(PROBE), run_name="probe_daily_ohlcv_test")
DailyOhlcv = NAMESPACE["DailyOhlcv"]
select_daily_ohlcv = NAMESPACE["select_daily_ohlcv"]
write_ohlcv = NAMESPACE["write_ohlcv"]


def epoch_ms(year: int, month: int, day: int) -> int:
    value = datetime(year, month, day, tzinfo=ET)
    return int(value.timestamp() * 1000)


def candle(year: int, month: int, day: int) -> dict[str, object]:
    return {
        "datetime": epoch_ms(year, month, day),
        "open": 1.25,
        "high": 1.75,
        "low": 1.00,
        "close": 1.50,
        "volume": 123456,
    }


class TestDailyOhlcvProbe(unittest.TestCase):

    def test_selects_exact_eastern_session_date(self) -> None:
        payload = {
            "candles": [
                candle(2026, 8, 7),
                candle(2026, 8, 10),
                candle(2026, 8, 11),
            ]
        }

        result = select_daily_ohlcv(
            payload,
            symbol=" daic ",
            trade_date=date(2026, 8, 10),
        )

        self.assertEqual(result.symbol, "DAIC")
        self.assertEqual(result.session_date, "2026-08-10")
        self.assertEqual(result.open, 1.25)
        self.assertEqual(result.high, 1.75)
        self.assertEqual(result.low, 1.00)
        self.assertEqual(result.close, 1.50)
        self.assertEqual(result.volume, 123456)
        self.assertEqual(
            result.datetime_et,
            "2026-08-10T00:00:00-04:00",
        )

    def test_missing_date_lists_returned_dates(self) -> None:
        payload = {
            "candles": [
                candle(2026, 8, 7),
                candle(2026, 8, 11),
            ]
        }

        with self.assertRaisesRegex(
            ValueError,
            "available dates: 2026-08-07, 2026-08-11",
        ):
            select_daily_ohlcv(
                payload,
                symbol="DAIC",
                trade_date=date(2026, 8, 10),
            )

    def test_rejects_nonintegral_or_negative_volume(self) -> None:
        fractional = candle(2026, 8, 10)
        fractional["volume"] = 12.5
        with self.assertRaisesRegex(ValueError, "nonnegative integer"):
            select_daily_ohlcv(
                {"candles": [fractional]},
                symbol="DAIC",
                trade_date=date(2026, 8, 10),
            )

        negative = candle(2026, 8, 10)
        negative["volume"] = -1
        with self.assertRaisesRegex(ValueError, "nonnegative integer"):
            select_daily_ohlcv(
                {"candles": [negative]},
                symbol="DAIC",
                trade_date=date(2026, 8, 10),
            )

    def test_writes_one_evidence_row(self) -> None:
        result = select_daily_ohlcv(
            {"candles": [candle(2026, 8, 10)]},
            symbol="DAIC",
            trade_date=date(2026, 8, 10),
        )

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "daily_ohlcv.csv"
            write_ohlcv(path, result)
            with path.open(encoding="utf-8", newline="") as file:
                rows = list(csv.DictReader(file))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "DAIC")
        self.assertEqual(rows[0]["volume"], "123456")


if __name__ == "__main__":
    unittest.main()
