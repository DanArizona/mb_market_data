from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone

from mb_market_data.decision_snapshot import (
    DecisionSnapshotDataError,
    build_decision_snapshot,
)
from mb_market_data.schwab_quotes import (
    QuoteResult,
    QuoteStatus,
)
from mb_market_data.tos_watchlist import (
    OVDecisionStatus,
    TosWatchlistRow,
)


UTC = timezone.utc

TRADE_DATE = date(
    2026,
    8,
    10,
)

TOS_OBSERVED = datetime(
    2026,
    8,
    10,
    13,
    25,
    0,
    100_000,
    tzinfo=UTC,
)

REQUEST_STARTED = datetime(
    2026,
    8,
    10,
    13,
    25,
    0,
    200_000,
    tzinfo=UTC,
)

RESPONSE_RECEIVED = datetime(
    2026,
    8,
    10,
    13,
    25,
    0,
    600_000,
    tzinfo=UTC,
)


def epoch_ms(
    value: datetime,
) -> int:
    return int(
        value.timestamp()
        * 1000
    )


def make_tos_row(
    *,
    symbol: str = "SPY",
    ov_decision: int | None = 546_802,
    status: OVDecisionStatus = (
        OVDecisionStatus.NUMERIC
    ),
    raw: str = "546802.0",
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


def make_payload(
    *,
    symbol: str = "SPY",
) -> dict:
    return {
        "symbol": symbol,
        "assetMainType": "EQUITY",
        "assetSubType": "ETF",
        "realtime": True,

        "quote": {
            "bidPrice": 600.10,
            "askPrice": 600.12,
            "lastPrice": 600.11,
            "mark": 600.11,
            "closePrice": 598.50,
            "openPrice": 599.00,
            "highPrice": 601.00,
            "lowPrice": 597.50,

            "bidSize": 100,
            "askSize": 200,
            "lastSize": 50,

            "totalVolume": 603_383,
            "securityStatus": "Normal",

            "quoteTime": epoch_ms(
                datetime(
                    2026,
                    8,
                    10,
                    13,
                    25,
                    0,
                    tzinfo=UTC,
                )
            ),

            "tradeTime": epoch_ms(
                datetime(
                    2026,
                    8,
                    10,
                    13,
                    24,
                    59,
                    440_000,
                    tzinfo=UTC,
                )
            ),

            "bidTime": epoch_ms(
                datetime(
                    2026,
                    8,
                    10,
                    13,
                    24,
                    59,
                    800_000,
                    tzinfo=UTC,
                )
            ),

            "askTime": epoch_ms(
                datetime(
                    2026,
                    8,
                    10,
                    13,
                    24,
                    59,
                    900_000,
                    tzinfo=UTC,
                )
            ),
        },

        "regular": {
            "regularMarketLastPrice": 598.50,
            "regularMarketLastSize": 100,
            "regularMarketTradeTime": epoch_ms(
                datetime(
                    2026,
                    8,
                    7,
                    20,
                    0,
                    0,
                    tzinfo=UTC,
                )
            ),
        },

        "fundamental": {
            "sharesOutstanding": 1_058_282_116,
            "avg10DaysVolume": 56_272_855,
            "avg1YearVolume": 73_384_017,
        },

        "reference": {
            "exchange": "P",
            "exchangeName": "NYSE Arca",
            "description": "SPDR S&P 500 ETF",
        },
    }


def make_quote_result(
    *,
    symbol: str = "SPY",
    status: QuoteStatus = QuoteStatus.QUOTE,
    quote=None,
    response_received_at_utc=RESPONSE_RECEIVED,
) -> QuoteResult:
    if (
        quote is None
        and status == QuoteStatus.QUOTE
    ):
        quote = make_payload(
            symbol=symbol
        )

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
        request_started_at_utc=(
            REQUEST_STARTED
        ),
        response_received_at_utc=(
            response_received_at_utc
        ),
    )


class TestDecisionSnapshot(unittest.TestCase):

    def test_builds_full_snapshot(self) -> None:
        snapshot = build_decision_snapshot(
            trade_date=TRADE_DATE,
            tos_row=make_tos_row(),
            quote_result=make_quote_result(),
            tos_observed_at_utc=TOS_OBSERVED,
        )

        self.assertEqual(
            snapshot.trade_date,
            TRADE_DATE,
        )

        self.assertEqual(
            snapshot.symbol,
            "SPY",
        )

        self.assertEqual(
            snapshot.ov_decision,
            546_802,
        )

        self.assertTrue(
            snapshot.has_usable_ov_decision
        )

        self.assertTrue(
            snapshot.has_schwab_quote
        )

        self.assertEqual(
            snapshot.quote_status,
            QuoteStatus.QUOTE,
        )

        self.assertEqual(
            snapshot.bid_price,
            600.10,
        )

        self.assertEqual(
            snapshot.ask_price,
            600.12,
        )

        self.assertEqual(
            snapshot.last_price,
            600.11,
        )

        self.assertEqual(
            snapshot.mark,
            600.11,
        )

        self.assertEqual(
            snapshot.close_price,
            598.50,
        )

        self.assertEqual(
            snapshot.total_volume,
            603_383,
        )

        self.assertEqual(
            snapshot.shares_outstanding,
            1_058_282_116,
        )

        self.assertEqual(
            snapshot.avg_10_days_volume,
            56_272_855.0,
        )

        self.assertEqual(
            snapshot.avg_1_year_volume,
            73_384_017.0,
        )

        self.assertEqual(
            snapshot.exchange,
            "P",
        )

        self.assertEqual(
            snapshot.exchange_name,
            "NYSE Arca",
        )

        self.assertEqual(
            snapshot.description,
            "SPDR S&P 500 ETF",
        )

        self.assertEqual(
            snapshot.tos_observed_at_utc,
            TOS_OBSERVED,
        )

        self.assertEqual(
            snapshot.schwab_request_started_at_utc,
            REQUEST_STARTED,
        )

        self.assertEqual(
            snapshot.schwab_response_received_at_utc,
            RESPONSE_RECEIVED,
        )

    def test_converts_market_event_times_to_utc(
        self,
    ) -> None:
        snapshot = build_decision_snapshot(
            trade_date=TRADE_DATE,
            tos_row=make_tos_row(),
            quote_result=make_quote_result(),
            tos_observed_at_utc=TOS_OBSERVED,
        )

        self.assertEqual(
            snapshot.quote_time_utc,
            datetime(
                2026,
                8,
                10,
                13,
                25,
                0,
                tzinfo=UTC,
            ),
        )

        self.assertEqual(
            snapshot.trade_time_utc,
            datetime(
                2026,
                8,
                10,
                13,
                24,
                59,
                440_000,
                tzinfo=UTC,
            ),
        )

    def test_missing_optional_fields_are_none(
        self,
    ) -> None:
        payload = {
            "symbol": "SPY",
            "quote": {
                "lastPrice": 600.0,
            },
        }

        snapshot = build_decision_snapshot(
            trade_date=TRADE_DATE,
            tos_row=make_tos_row(),
            quote_result=make_quote_result(
                quote=payload
            ),
            tos_observed_at_utc=TOS_OBSERVED,
        )

        self.assertEqual(
            snapshot.last_price,
            600.0,
        )

        self.assertIsNone(
            snapshot.bid_price
        )

        self.assertIsNone(
            snapshot.shares_outstanding
        )

        self.assertIsNone(
            snapshot.exchange
        )

    def test_unavailable_ov_decision_is_preserved(
        self,
    ) -> None:
        tos_row = make_tos_row(
            ov_decision=None,
            status=OVDecisionStatus.LOADING,
            raw="loading",
        )

        snapshot = build_decision_snapshot(
            trade_date=TRADE_DATE,
            tos_row=tos_row,
            quote_result=make_quote_result(),
            tos_observed_at_utc=TOS_OBSERVED,
        )

        self.assertIsNone(
            snapshot.ov_decision
        )

        self.assertEqual(
            snapshot.ov_decision_status,
            OVDecisionStatus.LOADING,
        )

        self.assertFalse(
            snapshot.has_usable_ov_decision
        )

        self.assertTrue(
            snapshot.has_schwab_quote
        )

    def test_invalid_schwab_symbol_is_preserved(
        self,
    ) -> None:
        quote_result = make_quote_result(
            status=QuoteStatus.INVALID,
            quote=None,
        )

        snapshot = build_decision_snapshot(
            trade_date=TRADE_DATE,
            tos_row=make_tos_row(),
            quote_result=quote_result,
            tos_observed_at_utc=TOS_OBSERVED,
        )

        self.assertEqual(
            snapshot.quote_status,
            QuoteStatus.INVALID,
        )

        self.assertFalse(
            snapshot.has_schwab_quote
        )

        self.assertIsNone(
            snapshot.last_price
        )

        self.assertIsNone(
            snapshot.shares_outstanding
        )

        self.assertEqual(
            snapshot.quote_detail,
            "test detail",
        )

    def test_request_error_without_response_time(
        self,
    ) -> None:
        quote_result = make_quote_result(
            status=QuoteStatus.REQUEST_ERROR,
            quote=None,
            response_received_at_utc=None,
        )

        snapshot = build_decision_snapshot(
            trade_date=TRADE_DATE,
            tos_row=make_tos_row(),
            quote_result=quote_result,
            tos_observed_at_utc=TOS_OBSERVED,
        )

        self.assertEqual(
            snapshot.quote_status,
            QuoteStatus.REQUEST_ERROR,
        )

        self.assertIsNone(
            snapshot.schwab_response_received_at_utc
        )

    def test_symbol_mismatch_rejected(self) -> None:
        with self.assertRaises(
            ValueError
        ):
            build_decision_snapshot(
                trade_date=TRADE_DATE,
                tos_row=make_tos_row(
                    symbol="SPY"
                ),
                quote_result=make_quote_result(
                    symbol="AAPL"
                ),
                tos_observed_at_utc=TOS_OBSERVED,
            )

    def test_naive_tos_observed_time_rejected(
        self,
    ) -> None:
        naive_time = datetime(
            2026,
            8,
            10,
            9,
            25,
        )

        with self.assertRaises(
            ValueError
        ):
            build_decision_snapshot(
                trade_date=TRADE_DATE,
                tos_row=make_tos_row(),
                quote_result=make_quote_result(),
                tos_observed_at_utc=naive_time,
            )

    def test_fractional_total_volume_rejected(
        self,
    ) -> None:
        payload = make_payload()

        payload["quote"]["totalVolume"] = 12.5

        with self.assertRaises(
            DecisionSnapshotDataError
        ):
            build_decision_snapshot(
                trade_date=TRADE_DATE,
                tos_row=make_tos_row(),
                quote_result=make_quote_result(
                    quote=payload
                ),
                tos_observed_at_utc=TOS_OBSERVED,
            )

    def test_integral_float_shares_accepted(
        self,
    ) -> None:
        payload = make_payload()

        payload["fundamental"][
            "sharesOutstanding"
        ] = 1_058_282_116.0

        snapshot = build_decision_snapshot(
            trade_date=TRADE_DATE,
            tos_row=make_tos_row(),
            quote_result=make_quote_result(
                quote=payload
            ),
            tos_observed_at_utc=TOS_OBSERVED,
        )

        self.assertEqual(
            snapshot.shares_outstanding,
            1_058_282_116,
        )

    def test_snapshot_is_frozen(self) -> None:
        snapshot = build_decision_snapshot(
            trade_date=TRADE_DATE,
            tos_row=make_tos_row(),
            quote_result=make_quote_result(),
            tos_observed_at_utc=TOS_OBSERVED,
        )

        with self.assertRaises(
            FrozenInstanceError
        ):
            snapshot.symbol = "AAPL"


if __name__ == "__main__":
    unittest.main()
