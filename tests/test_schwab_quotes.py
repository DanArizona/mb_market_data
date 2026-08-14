from __future__ import annotations

import unittest

from mb_market_data.schwab_quotes import (
    DEFAULT_QUOTE_BATCH_SIZE,
    QuoteStatus,
    fetch_quotes_batched,
    normalize_symbols,
)


class FakeResponse:

    def __init__(
        self,
        payload,
        *,
        ok: bool = True,
        status_code: int = 200,
        json_error: Exception | None = None,
    ) -> None:
        self.payload = payload
        self.ok = ok
        self.status_code = status_code
        self.json_error = json_error

    def json(self):
        if self.json_error is not None:
            raise self.json_error

        return self.payload


class FakeClient:

    def __init__(
        self,
        responses,
    ) -> None:
        self.responses = list(responses)
        self.calls = []

    def quotes(
        self,
        symbols,
        *,
        fields="quote",
    ):
        self.calls.append(
            {
                "symbols": list(symbols),
                "fields": fields,
            }
        )

        return self.responses.pop(0)


class EchoClient:

    def __init__(self) -> None:
        self.calls = []

    def quotes(
        self,
        symbols,
        *,
        fields="quote",
    ):
        symbols = list(symbols)

        self.calls.append(
            {
                "symbols": symbols,
                "fields": fields,
            }
        )

        payload = {
            symbol: {
                "symbol": symbol,
            }
            for symbol in symbols
        }

        return FakeResponse(payload)


class TestNormalizeSymbols(unittest.TestCase):

    def test_normalizes_and_deduplicates(self) -> None:
        result = normalize_symbols(
            [
                " spy ",
                "AAPL",
                "spy",
                "",
                " msft",
            ]
        )

        self.assertEqual(
            result,
            (
                "SPY",
                "AAPL",
                "MSFT",
            ),
        )

    def test_comma_separated_string(self) -> None:
        result = normalize_symbols(
            "SPY,AAPL, MSFT"
        )

        self.assertEqual(
            result,
            (
                "SPY",
                "AAPL",
                "MSFT",
            ),
        )


class TestFetchQuotesBatched(unittest.TestCase):

    def test_default_batch_size_is_400(self) -> None:
        self.assertEqual(
            DEFAULT_QUOTE_BATCH_SIZE,
            400,
        )

    def test_759_symbols_use_two_requests(self) -> None:
        symbols = [
            f"S{index:03d}"
            for index in range(759)
        ]

        client = EchoClient()

        result = fetch_quotes_batched(
            client,
            symbols,
        )

        self.assertEqual(
            result.request_count,
            2,
        )

        self.assertEqual(
            len(client.calls[0]["symbols"]),
            400,
        )

        self.assertEqual(
            len(client.calls[1]["symbols"]),
            359,
        )

        self.assertEqual(
            len(result.results),
            759,
        )

        self.assertTrue(
            all(
                item.status
                == QuoteStatus.QUOTE
                for item in result.results
            )
        )

    def test_invalid_symbol_is_preserved(self) -> None:
        client = FakeClient(
            [
                FakeResponse(
                    {
                        "SPY": {
                            "symbol": "SPY",
                        },
                        "AAPL": {
                            "symbol": "AAPL",
                        },
                        "errors": {
                            "invalidSymbols": [
                                "BAD"
                            ]
                        },
                    }
                )
            ]
        )

        result = fetch_quotes_batched(
            client,
            [
                "SPY",
                "BAD",
                "AAPL",
            ],
        )

        by_symbol = result.by_symbol()

        self.assertEqual(
            by_symbol["SPY"].status,
            QuoteStatus.QUOTE,
        )

        self.assertEqual(
            by_symbol["BAD"].status,
            QuoteStatus.INVALID,
        )

        self.assertEqual(
            by_symbol["AAPL"].status,
            QuoteStatus.QUOTE,
        )

    def test_unaccounted_symbol_is_missing(self) -> None:
        client = FakeClient(
            [
                FakeResponse(
                    {
                        "SPY": {
                            "symbol": "SPY",
                        }
                    }
                )
            ]
        )

        result = fetch_quotes_batched(
            client,
            [
                "SPY",
                "AAPL",
            ],
        )

        by_symbol = result.by_symbol()

        self.assertEqual(
            by_symbol["AAPL"].status,
            QuoteStatus.MISSING,
        )

    def test_http_failure_marks_batch_and_continues(self) -> None:
        client = FakeClient(
            [
                FakeResponse(
                    {
                        "errors": [
                            {
                                "detail":
                                    "Temporary failure"
                            }
                        ]
                    },
                    ok=False,
                    status_code=500,
                ),
                FakeResponse(
                    {
                        "C": {
                            "symbol": "C",
                        },
                        "D": {
                            "symbol": "D",
                        },
                    }
                ),
            ]
        )

        result = fetch_quotes_batched(
            client,
            [
                "A",
                "B",
                "C",
                "D",
            ],
            batch_size=2,
        )

        by_symbol = result.by_symbol()

        self.assertEqual(
            by_symbol["A"].status,
            QuoteStatus.REQUEST_ERROR,
        )

        self.assertEqual(
            by_symbol["B"].status,
            QuoteStatus.REQUEST_ERROR,
        )

        self.assertEqual(
            by_symbol["C"].status,
            QuoteStatus.QUOTE,
        )

        self.assertEqual(
            by_symbol["D"].status,
            QuoteStatus.QUOTE,
        )

        self.assertEqual(
            result.request_count,
            2,
        )

    def test_unreadable_json_marks_request_error(self) -> None:
        client = FakeClient(
            [
                FakeResponse(
                    None,
                    json_error=ValueError(
                        "bad json"
                    ),
                )
            ]
        )

        result = fetch_quotes_batched(
            client,
            ["SPY"],
        )

        self.assertEqual(
            result.results[0].status,
            QuoteStatus.REQUEST_ERROR,
        )

    def test_batch_size_above_500_rejected(self) -> None:
        client = EchoClient()

        with self.assertRaises(
            ValueError
        ):
            fetch_quotes_batched(
                client,
                ["SPY"],
                batch_size=501,
            )

    def test_empty_input_makes_no_requests(self) -> None:
        client = EchoClient()

        result = fetch_quotes_batched(
            client,
            [],
        )

        self.assertEqual(
            result.request_count,
            0,
        )

        self.assertEqual(
            result.results,
            (),
        )

        self.assertEqual(
            client.calls,
            [],
        )


if __name__ == "__main__":
    unittest.main()
