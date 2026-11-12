from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from mb_market_data.decision_batch import (
    DecisionBatchError,
    build_decision_snapshot_batch,
)
from mb_market_data.schwab_quotes import (
    QuoteBatchResult,
    QuoteResult,
    QuoteStatus,
)
from mb_market_data.tos_watchlist import (
    OVDecisionStatus,
    TosWatchlist,
    TosWatchlistRow,
)


UTC = timezone.utc

TRADE_DATE = date(2026, 8, 10)

OBSERVED = datetime(
    2026,
    8,
    10,
    13,
    25,
    1,
    tzinfo=UTC,
)

REQUESTED = datetime(
    2026,
    8,
    10,
    13,
    25,
    2,
    tzinfo=UTC,
)

RECEIVED = datetime(
    2026,
    8,
    10,
    13,
    25,
    3,
    tzinfo=UTC,
)


def make_tos_row(
    symbol: str,
    *,
    ov_decision: int | None = 1000,
    status: OVDecisionStatus = (
        OVDecisionStatus.NUMERIC
    ),
    raw: str = "1000",
) -> TosWatchlistRow:
    return TosWatchlistRow(
        symbol=symbol,
        ov_decision=ov_decision,
        ov_decision_status=status,
        raw_ov_decision=raw,
        fields={
            "Symbol": symbol,
            "OV_DECISION": raw,
        },
    )


def make_watchlist(
    rows,
) -> TosWatchlist:
    return TosWatchlist(
        path=Path("test.csv"),
        headers=(
            "Symbol",
            "OV_DECISION",
        ),
        header_line_number=4,
        rows=tuple(rows),
    )


def make_quote_result(
    symbol: str,
    *,
    status: QuoteStatus = QuoteStatus.QUOTE,
) -> QuoteResult:
    quote = None

    if status == QuoteStatus.QUOTE:
        quote = {
            "symbol": symbol,
            "assetMainType": "EQUITY",
            "quote": {
                "lastPrice": 10.0,
                "totalVolume": 1000,
            },
        }

    return QuoteResult(
        symbol=symbol,
        status=status,
        quote=quote,
        detail=(
            None
            if status == QuoteStatus.QUOTE
            else "test detail"
        ),
        batch_number=1,
        request_started_at_utc=REQUESTED,
        response_received_at_utc=RECEIVED,
    )


def make_quote_batch(
    results,
) -> QuoteBatchResult:
    return QuoteBatchResult(
        results=tuple(results),
        request_count=1,
        batch_size=400,
        unexpected_symbols=(),
    )


class TestDecisionBatch(unittest.TestCase):

    def test_builds_one_snapshot_per_tos_row(
        self,
    ) -> None:
        watchlist = make_watchlist(
            [
                make_tos_row("AAA"),
                make_tos_row("BBB"),
                make_tos_row("CCC"),
            ]
        )

        quotes = make_quote_batch(
            [
                make_quote_result("AAA"),
                make_quote_result("BBB"),
                make_quote_result("CCC"),
            ]
        )

        batch = build_decision_snapshot_batch(
            trade_date=TRADE_DATE,
            watchlist=watchlist,
            quote_batch=quotes,
            tos_observed_at_utc=OBSERVED,
        )

        self.assertEqual(
            len(batch.snapshots),
            3,
        )

        self.assertEqual(
            [
                item.symbol
                for item in batch.snapshots
            ],
            [
                "AAA",
                "BBB",
                "CCC",
            ],
        )

        self.assertEqual(
            batch.quote_request_count,
            1,
        )

        self.assertEqual(
            batch.quote_batch_size,
            400,
        )

    def test_source_readiness_is_preserved(
        self,
    ) -> None:
        watchlist = make_watchlist(
            [
                make_tos_row("GOOD"),
                make_tos_row(
                    "LOADING",
                    ov_decision=None,
                    status=OVDecisionStatus.LOADING,
                    raw="loading",
                ),
                make_tos_row("INVALID"),
            ]
        )

        quotes = make_quote_batch(
            [
                make_quote_result("GOOD"),
                make_quote_result("LOADING"),
                make_quote_result(
                    "INVALID",
                    status=QuoteStatus.INVALID,
                ),
            ]
        )

        batch = build_decision_snapshot_batch(
            trade_date=TRADE_DATE,
            watchlist=watchlist,
            quote_batch=quotes,
            tos_observed_at_utc=OBSERVED,
        )

        self.assertEqual(
            [
                item.symbol
                for item
                in batch.source_ready_snapshots
            ],
            ["GOOD"],
        )

        self.assertEqual(
            {
                item.symbol
                for item
                in batch.source_unready_snapshots
            },
            {
                "LOADING",
                "INVALID",
            },
        )

    def test_missing_quote_result_raises(
        self,
    ) -> None:
        watchlist = make_watchlist(
            [
                make_tos_row("AAA"),
                make_tos_row("BBB"),
            ]
        )

        quotes = make_quote_batch(
            [
                make_quote_result("AAA"),
            ]
        )

        with self.assertRaisesRegex(
            DecisionBatchError,
            "BBB",
        ):
            build_decision_snapshot_batch(
                trade_date=TRADE_DATE,
                watchlist=watchlist,
                quote_batch=quotes,
                tos_observed_at_utc=OBSERVED,
            )

    def test_duplicate_tos_symbol_raises(
        self,
    ) -> None:
        watchlist = make_watchlist(
            [
                make_tos_row("AAA"),
                make_tos_row("AAA"),
            ]
        )

        quotes = make_quote_batch(
            [
                make_quote_result("AAA"),
            ]
        )

        with self.assertRaisesRegex(
            DecisionBatchError,
            "duplicate",
        ):
            build_decision_snapshot_batch(
                trade_date=TRADE_DATE,
                watchlist=watchlist,
                quote_batch=quotes,
                tos_observed_at_utc=OBSERVED,
            )

    def test_extra_quote_result_is_reported(
        self,
    ) -> None:
        watchlist = make_watchlist(
            [
                make_tos_row("AAA"),
            ]
        )

        quotes = make_quote_batch(
            [
                make_quote_result("AAA"),
                make_quote_result("EXTRA"),
            ]
        )

        batch = build_decision_snapshot_batch(
            trade_date=TRADE_DATE,
            watchlist=watchlist,
            quote_batch=quotes,
            tos_observed_at_utc=OBSERVED,
        )

        self.assertEqual(
            batch.quote_results_not_in_watchlist,
            ("EXTRA",),
        )


if __name__ == "__main__":
    unittest.main()
