from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from mb_market_data.daily_universe_snapshot import (
    build_market_data_snapshot,
    validate_acquisition_time,
    write_acquisition_json,
    write_snapshot_csv,
    write_snapshot_manifest,
)
from mb_market_data.schwab_quotes import (
    QuoteBatchResult,
    QuoteResult,
    QuoteStatus,
)


UTC = timezone.utc


def candidate(symbol: str) -> dict[str, str]:
    return {
        "symbol": symbol,
        "security_name": f"{symbol} Incorporated",
        "source": "nasdaqlisted",
        "listing_exchange_code": "Q",
        "listing_exchange_name": "NASDAQ",
        "market_category": "G",
        "test_issue": "N",
        "financial_status": "N",
        "etf": "N",
        "round_lot_size": "100",
        "cqs_symbol": "",
        "nasdaq_symbol": symbol,
    }


def quote_result(
    symbol: str,
    *,
    regular_trade_at: datetime,
    status: QuoteStatus = QuoteStatus.QUOTE,
) -> QuoteResult:
    timestamp_ms = int(regular_trade_at.timestamp() * 1000)
    quote = None
    if status == QuoteStatus.QUOTE:
        quote = {
            "assetMainType": "EQUITY",
            "assetSubType": "COE",
            "quote": {
                "closePrice": 5.3,
                "lastPrice": 3.58,
                "totalVolume": 4_100_973,
            },
            "regular": {
                "regularMarketLastPrice": 3.55,
                "regularMarketTradeTime": timestamp_ms,
            },
            "fundamental": {
                "sharesOutstanding": 1_210_383,
                "marketCap": 2_432_869,
            },
        }
    return QuoteResult(
        symbol=symbol,
        status=status,
        quote=quote,
        detail=None if quote is not None else "unavailable",
        batch_number=1,
        request_started_at_utc=datetime(2026, 9, 18, 23, 52, tzinfo=UTC),
        response_received_at_utc=datetime(2026, 9, 18, 23, 52, 1, tzinfo=UTC),
    )


def acquisition(*results: QuoteResult) -> QuoteBatchResult:
    return QuoteBatchResult(
        results=results,
        request_count=1,
        batch_size=400,
        unexpected_symbols=(),
    )


class TestBuildMarketDataSnapshot(unittest.TestCase):

    def test_acquisition_time_requires_completed_regular_session(self) -> None:
        with self.assertRaisesRegex(ValueError, "at or after 16:00 ET"):
            validate_acquisition_time(
                date(2026, 9, 18),
                datetime(2026, 9, 18, 19, 59, tzinfo=UTC),
            )

        validate_acquisition_time(
            date(2026, 9, 18),
            datetime(2026, 9, 18, 20, 0, tzinfo=UTC),
        )
        validate_acquisition_time(
            date(2026, 9, 18),
            datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
        )

    def test_extracts_daic_regular_session_inputs(self) -> None:
        regular_trade = datetime(2026, 9, 18, 20, 0, 0, 596000, tzinfo=UTC)
        rows = build_market_data_snapshot(
            [candidate("DAIC")],
            acquisition(quote_result("DAIC", regular_trade_at=regular_trade)),
            session_date=date(2026, 9, 18),
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["symbol"], "DAIC")
        self.assertEqual(row["acquisition_status"], "quote")
        self.assertEqual(row["close_price"], "3.55")
        self.assertEqual(row["total_volume"], "4100973")
        self.assertEqual(row["shares_outstanding"], "1210383")
        self.assertEqual(row["calculated_market_cap"], "4296859.65")
        self.assertEqual(row["direct_market_cap"], "2432869")
        self.assertEqual(row["quote_close_price"], "5.3")
        self.assertEqual(row["quote_last_price"], "3.58")
        self.assertEqual(row["regular_market_session_match"], "true")
        self.assertEqual(
            row["regular_market_trade_time_et"],
            "2026-09-18T16:00:00.596000-04:00",
        )

    def test_marks_prior_session_trade_as_stale(self) -> None:
        regular_trade = datetime(2026, 9, 17, 20, 0, tzinfo=UTC)
        row = build_market_data_snapshot(
            [candidate("STALE")],
            acquisition(quote_result("STALE", regular_trade_at=regular_trade)),
            session_date=date(2026, 9, 18),
        )[0]

        self.assertEqual(row["regular_market_session_match"], "false")

    def test_preserves_explicit_unavailable_result(self) -> None:
        result = quote_result(
            "BAD",
            regular_trade_at=datetime(2026, 9, 18, 20, tzinfo=UTC),
            status=QuoteStatus.INVALID,
        )
        row = build_market_data_snapshot(
            [candidate("BAD")],
            acquisition(result),
            session_date=date(2026, 9, 18),
        )[0]

        self.assertEqual(row["acquisition_status"], "invalid")
        self.assertEqual(row["acquisition_detail"], "unavailable")
        self.assertEqual(row["close_price"], "")
        self.assertEqual(row["regular_market_session_match"], "missing")

    def test_rejects_result_set_that_does_not_match_candidates(self) -> None:
        regular_trade = datetime(2026, 9, 18, 20, tzinfo=UTC)
        with self.assertRaisesRegex(ValueError, "do not match candidates"):
            build_market_data_snapshot(
                [candidate("EXPECTED")],
                acquisition(
                    quote_result("OTHER", regular_trade_at=regular_trade)
                ),
                session_date=date(2026, 9, 18),
            )

    def test_writes_snapshot_raw_acquisition_and_manifest(self) -> None:
        regular_trade = datetime(2026, 9, 18, 20, tzinfo=UTC)
        batch = acquisition(
            quote_result("DAIC", regular_trade_at=regular_trade)
        )
        rows = build_market_data_snapshot(
            [candidate("DAIC")],
            batch,
            session_date=date(2026, 9, 18),
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate_path = root / "candidates.csv"
            candidate_path.write_text("symbol\nDAIC\n", encoding="utf-8")
            snapshot_path = root / "snapshot.csv"
            acquisition_path = root / "acquisition.json"
            manifest_path = root / "manifest.json"

            write_snapshot_csv(snapshot_path, rows)
            write_acquisition_json(
                acquisition_path,
                batch,
                session_date=date(2026, 9, 18),
            )
            write_snapshot_manifest(
                manifest_path,
                session_date=date(2026, 9, 18),
                candidate_path=candidate_path,
                snapshot_path=snapshot_path,
                acquisition_path=acquisition_path,
                acquisition=batch,
                rows=rows,
                created_at_utc=datetime(2026, 9, 19, tzinfo=UTC),
            )

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            raw = json.loads(acquisition_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["acquisition"]["status_counts"], {"quote": 1})
        self.assertEqual(
            manifest["acquisition"]["regular_session_match_counts"],
            {"true": 1},
        )
        self.assertEqual(raw["results"][0]["symbol"], "DAIC")


if __name__ == "__main__":
    unittest.main()
