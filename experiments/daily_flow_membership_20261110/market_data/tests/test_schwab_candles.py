from __future__ import annotations

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from mb_market_data.schwab_candles import (
    CandleDataError,
    CandleNotFoundError,
    PriceHistoryRequestError,
    candle_datetime_et,
    fetch_0925_candle,
    find_0925_candle,
)


ET = ZoneInfo("America/New_York")


def epoch_ms(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
) -> int:
    dt = datetime(
        year,
        month,
        day,
        hour,
        minute,
        tzinfo=ET,
    )

    return int(
        dt.timestamp() * 1000
    )


def make_candle(
    hour: int,
    minute: int,
    *,
    volume: int = 100,
) -> dict:
    return {
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": volume,
        "datetime": epoch_ms(
            2026,
            8,
            10,
            hour,
            minute,
        ),
    }


class FakeResponse:

    def __init__(
        self,
        payload,
        *,
        ok: bool = True,
        status_code: int = 200,
    ) -> None:
        self._payload = payload
        self.ok = ok
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeClient:

    def __init__(
        self,
        response: FakeResponse,
    ) -> None:
        self.response = response
        self.calls = []

    def price_history(
        self,
        symbol,
        **kwargs,
    ):
        self.calls.append(
            (symbol, kwargs)
        )

        return self.response


class TestCandleDatetimeET(unittest.TestCase):

    def test_epoch_converts_to_et(self) -> None:
        value = epoch_ms(
            2026,
            8,
            10,
            9,
            25,
        )

        result = candle_datetime_et(
            value
        )

        self.assertEqual(
            result.hour,
            9,
        )

        self.assertEqual(
            result.minute,
            25,
        )

        self.assertEqual(
            result.date(),
            date(2026, 8, 10),
        )


class TestFind0925Candle(unittest.TestCase):

    def test_finds_exact_0925_candle(self) -> None:
        payload = {
            "candles": [
                make_candle(9, 20, volume=111),
                make_candle(9, 25, volume=60_848),
                make_candle(9, 30, volume=333),
            ]
        }

        candle = find_0925_candle(
            payload,
            symbol="spy",
            trade_date=date(
                2026,
                8,
                10,
            ),
        )

        self.assertEqual(
            candle.symbol,
            "SPY",
        )

        self.assertEqual(
            candle.start_et.hour,
            9,
        )

        self.assertEqual(
            candle.start_et.minute,
            25,
        )

        self.assertEqual(
            candle.volume,
            60_848,
        )

    def test_wrong_date_does_not_match(self) -> None:
        payload = {
            "candles": [
                make_candle(
                    9,
                    25,
                    volume=60_848,
                )
            ]
        }

        with self.assertRaises(
            CandleNotFoundError
        ):
            find_0925_candle(
                payload,
                symbol="SPY",
                trade_date=date(
                    2026,
                    8,
                    11,
                ),
            )

    def test_missing_0925_candle_raises(self) -> None:
        payload = {
            "candles": [
                make_candle(9, 20),
                make_candle(9, 30),
            ]
        }

        with self.assertRaises(
            CandleNotFoundError
        ):
            find_0925_candle(
                payload,
                symbol="SPY",
                trade_date=date(
                    2026,
                    8,
                    10,
                ),
            )

    def test_missing_candle_list_raises(self) -> None:
        with self.assertRaises(
            CandleDataError
        ):
            find_0925_candle(
                {},
                symbol="SPY",
                trade_date=date(
                    2026,
                    8,
                    10,
                ),
            )

    def test_fractional_volume_rejected(self) -> None:
        candle = make_candle(
            9,
            25,
        )

        candle["volume"] = 12.5

        payload = {
            "candles": [
                candle
            ]
        }

        with self.assertRaises(
            CandleDataError
        ):
            find_0925_candle(
                payload,
                symbol="SPY",
                trade_date=date(
                    2026,
                    8,
                    10,
                ),
            )

    def test_integral_float_volume_accepted(self) -> None:
        candle = make_candle(
            9,
            25,
        )

        candle["volume"] = 60_848.0

        payload = {
            "candles": [
                candle
            ]
        }

        result = find_0925_candle(
            payload,
            symbol="SPY",
            trade_date=date(
                2026,
                8,
                10,
            ),
        )

        self.assertEqual(
            result.volume,
            60_848,
        )


class TestFetch0925Candle(unittest.TestCase):

    def test_fetches_using_expected_parameters(self) -> None:
        payload = {
            "candles": [
                make_candle(
                    9,
                    25,
                    volume=60_848,
                )
            ]
        }

        client = FakeClient(
            FakeResponse(payload)
        )

        candle = fetch_0925_candle(
            client,
            symbol="spy",
            trade_date=date(
                2026,
                8,
                10,
            ),
        )

        self.assertEqual(
            candle.volume,
            60_848,
        )

        self.assertEqual(
            len(client.calls),
            1,
        )

        symbol, kwargs = client.calls[0]

        self.assertEqual(
            symbol,
            "SPY",
        )

        self.assertEqual(
            kwargs["frequencyType"],
            "minute",
        )

        self.assertEqual(
            kwargs["frequency"],
            5,
        )

        self.assertTrue(
            kwargs["needExtendedHoursData"]
        )

        self.assertTrue(
            kwargs["needPreviousClose"]
        )

        self.assertEqual(
            kwargs["startDate"].tzinfo,
            ET,
        )

        self.assertEqual(
            kwargs["endDate"].tzinfo,
            ET,
        )

    def test_http_failure_raises(self) -> None:
        client = FakeClient(
            FakeResponse(
                {},
                ok=False,
                status_code=500,
            )
        )

        with self.assertRaises(
            PriceHistoryRequestError
        ):
            fetch_0925_candle(
                client,
                symbol="SPY",
                trade_date=date(
                    2026,
                    8,
                    10,
                ),
            )


if __name__ == "__main__":
    unittest.main()
