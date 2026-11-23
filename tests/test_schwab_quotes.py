from __future__ import annotations

import unittest
from datetime import timezone
from hashlib import sha256
import json

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
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.payload = payload
        self.ok = ok
        self.status_code = status_code
        self.json_error = json_error
        self.content = content
        self.headers = headers or {}
        self.json_calls = 0

    def json(self):
        self.json_calls += 1
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


class RaisingClient:

    def quotes(
        self,
        symbols,
        *,
        fields="quote",
    ):
        raise TimeoutError(
            "simulated timeout"
        )


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

        first = result.results[0]
        last_first_batch = result.results[399]
        first_second_batch = result.results[400]

        self.assertEqual(
            first.request_started_at_utc.tzinfo,
            timezone.utc,
        )

        self.assertIsNotNone(
            first.response_received_at_utc
        )

        self.assertEqual(
            first.response_received_at_utc.tzinfo,
            timezone.utc,
        )

        # Every result from the same HTTP request carries
        # the same acquisition timestamps.
        self.assertEqual(
            first.request_started_at_utc,
            last_first_batch.request_started_at_utc,
        )

        self.assertEqual(
            first.response_received_at_utc,
            last_first_batch.response_received_at_utc,
        )

        # The second batch has its own batch number and
        # its own acquisition timing.
        self.assertEqual(
            first_second_batch.batch_number,
            2,
        )

        self.assertEqual(
            first_second_batch.request_started_at_utc.tzinfo,
            timezone.utc,
        )

        self.assertIsNotNone(
            first_second_batch.response_received_at_utc
        )

        self.assertEqual(
            first_second_batch.response_received_at_utc.tzinfo,
            timezone.utc,
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


    def test_bad_json_records_status_type_hash_and_batch_size_without_secrets(self) -> None:
        secret = "Bearer DO_NOT_LOG_SECRET"
        body = (secret + "\\nnot-json").encode("utf-8")
        response = FakeResponse(
            None,
            content=body,
            headers={"Content-Type": "text/html; private=" + secret,
                     "Authorization": secret},
            json_error=json.JSONDecodeError("Expecting value", "bad-json", 1),
        )
        result = fetch_quotes_batched(FakeClient([response]), ["SPY", "AAPL"])
        self.assertEqual(response.json_calls, 1)
        self.assertEqual(len(result.results), 2)
        self.assertEqual({r.status for r in result.results}, {QuoteStatus.REQUEST_ERROR})
        details = {r.detail for r in result.results}
        self.assertEqual(len(details), 1)
        detail = next(iter(details))
        for expected in (
            "Unreadable Schwab JSON response",
            "JSONDecodeError",
            "http_status=200",
            "content_type=text/html",
            f"body_bytes={len(body)}",
            f"body_sha256={sha256(body).hexdigest()}",
            "batch_symbols=2",
            "line=1",
        ):
            self.assertIn(expected, detail)
        self.assertNotIn(secret, detail)
        self.assertNotIn("Authorization", detail)
        self.assertNotIn("private=", detail)

    def test_non_json_http_failure_preserves_http_status_and_safe_metadata(self) -> None:
        secret = "PASSWORD_DO_NOT_LOG"
        body = ("<html>" + secret + "</html>").encode()
        response = FakeResponse(
            None,
            ok=False,
            status_code=503,
            content=body,
            headers={"Content-Type": "text/html; charset=UTF-8"},
            json_error=ValueError(secret),
        )
        result = fetch_quotes_batched(FakeClient([response]), ["SPY", "AMD"])
        self.assertEqual(response.json_calls, 1)
        self.assertEqual(result.request_count, 1)
        for item in result.results:
            self.assertEqual(item.status, QuoteStatus.REQUEST_ERROR)
            self.assertIsNotNone(item.response_received_at_utc)
            self.assertIn("HTTP 503", item.detail)
            self.assertIn("error_json=ValueError", item.detail)
            self.assertIn("http_status=503", item.detail)
            self.assertIn(f"body_sha256={sha256(body).hexdigest()}", item.detail)
            self.assertIn("batch_symbols=2", item.detail)
            self.assertNotIn(secret, item.detail)

    def test_valid_http_error_keeps_structured_provider_detail(self) -> None:
        response = FakeResponse(
            {"errors": [{"detail": "Temporary failure"}]},
            ok=False,
            status_code=500,
            content=b'{"errors":[{"detail":"Temporary failure"}]}',
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        result = fetch_quotes_batched(FakeClient([response]), ["SPY"])
        self.assertEqual(response.json_calls, 1)
        self.assertEqual(result.results[0].status, QuoteStatus.REQUEST_ERROR)
        self.assertIn("HTTP 500: Temporary failure", result.results[0].detail)
        self.assertIn("content_type=application/json", result.results[0].detail)
        self.assertIn("http_status=500", result.results[0].detail)

    def test_four_hundred_bad_json_then_one_hundred_eighty_six_quotes_continue(self):
        first = [f"Q{n:03d}" for n in range(400)]
        second = [f"R{n:03d}" for n in range(186)]
        response = FakeResponse(
            None,
            content=b"not parseable",
            headers={"Content-Type": "application/json"},
            json_error=ValueError("bad json"),
        )
        client = FakeClient([
            response,
            FakeResponse({symbol: {"symbol": symbol} for symbol in second}),
        ])
        result = fetch_quotes_batched(client, first + second, batch_size=400)
        self.assertEqual(result.request_count, 2)
        self.assertEqual(len(result.results), 586)
        self.assertTrue(all(v.status == QuoteStatus.REQUEST_ERROR for v in result.results[:400]))
        self.assertTrue(all(v.status == QuoteStatus.QUOTE for v in result.results[400:]))
        self.assertIn("batch_symbols=400", result.results[0].detail)
        self.assertEqual(len(client.calls[0]["symbols"]), 400)
        self.assertEqual(len(client.calls[1]["symbols"]), 186)

    def test_client_exception_has_no_response_time(
        self,
    ) -> None:
        client = RaisingClient()

        result = fetch_quotes_batched(
            client,
            [
                "SPY",
                "AAPL",
            ],
        )

        self.assertEqual(
            len(result.results),
            2,
        )

        for item in result.results:
            self.assertEqual(
                item.status,
                QuoteStatus.REQUEST_ERROR,
            )

            self.assertEqual(
                item.request_started_at_utc.tzinfo,
                timezone.utc,
            )

            self.assertIsNone(
                item.response_received_at_utc
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
