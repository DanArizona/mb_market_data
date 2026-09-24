from __future__ import annotations

import csv
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from mb_market_data.api_overnight_volume import (
    ET,
    APIOvernightVolumeBatch,
    APIOvernightVolumeDataError,
    APIOvernightVolumeObservation,
    APIOvernightVolumeStatus,
    acquire_api_overnight_volume,
    parse_price_history_payload,
    write_api_overnight_volume_artifacts,
)


UTC = timezone.utc
TRADE_DATE = date(2026, 9, 21)


def epoch_ms(hour: int, minute: int, *, day: int = 21) -> int:
    value = datetime(2026, 9, day, hour, minute, tzinfo=ET)
    return int(value.timestamp() * 1000)


def candle(hour: int, minute: int, volume: int, *, day: int = 21) -> dict:
    return {
        "datetime": epoch_ms(hour, minute, day=day),
        "open": 1.0,
        "high": 1.2,
        "low": 0.9,
        "close": 1.1,
        "volume": volume,
    }


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: Any = None,
        headers: dict[str, str] | None = None,
        json_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._payload = payload
        self.headers = headers or {}
        self._json_error = json_error

    def json(self) -> Any:
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class FakeClient:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def price_history(self, symbol: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((symbol, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 21, 12, 25, tzinfo=UTC)

    def __call__(self) -> datetime:
        result = self.value
        self.value += timedelta(milliseconds=100)
        return result


class TestAPIOvernightVolumeParsing(unittest.TestCase):
    def test_uses_half_open_midnight_to_0900_et_window(self) -> None:
        payload = {
            "symbol": "ABCD",
            "candles": [
                candle(23, 55, 99, day=20),
                candle(0, 0, 10),
                candle(8, 20, 20),
                candle(8, 55, 30),
                candle(9, 0, 999),
            ],
        }

        selected, volume = parse_price_history_payload(
            payload,
            symbol="ABCD",
            trade_date=TRADE_DATE,
        )

        self.assertEqual([item.volume for item in selected], [10, 20, 30])
        self.assertEqual(volume, 60)

    def test_accepts_explicit_retrospective_0930_window(self) -> None:
        payload = {
            "symbol": "ABCD",
            "candles": [
                candle(8, 20, 10),
                candle(8, 25, 20),
                candle(9, 25, 30),
                candle(9, 30, 999),
            ],
        }

        selected, volume = parse_price_history_payload(
            payload,
            symbol="ABCD",
            trade_date=TRADE_DATE,
            window_end=time(9, 30),
        )

        self.assertEqual([item.volume for item in selected], [10, 20, 30])
        self.assertEqual(volume, 60)

    def test_empty_candle_list_is_valid_zero_volume(self) -> None:
        selected, volume = parse_price_history_payload(
            {"symbol": "ABCD", "candles": []},
            symbol="ABCD",
            trade_date=TRADE_DATE,
        )

        self.assertEqual(selected, ())
        self.assertEqual(volume, 0)

    def test_rejects_duplicate_selected_candle(self) -> None:
        duplicate = candle(7, 0, 10)
        with self.assertRaisesRegex(
            APIOvernightVolumeDataError, "duplicate candle start"
        ):
            parse_price_history_payload(
                {"symbol": "ABCD", "candles": [duplicate, duplicate]},
                symbol="ABCD",
                trade_date=TRADE_DATE,
            )

    def test_rejects_invalid_selected_volume(self) -> None:
        bad = candle(7, 0, 10)
        bad["volume"] = -1
        with self.assertRaisesRegex(
            APIOvernightVolumeDataError, "nonnegative integer"
        ):
            parse_price_history_payload(
                {"symbol": "ABCD", "candles": [bad]},
                symbol="ABCD",
                trade_date=TRADE_DATE,
            )


class TestAPIOvernightVolumeAcquisition(unittest.TestCase):
    def test_acquires_explicit_success_for_every_symbol(self) -> None:
        client = FakeClient(
            FakeResponse(payload={"symbol": "AAA", "candles": []}),
            FakeResponse(
                payload={"symbol": "BBB", "candles": [candle(7, 0, 50)]}
            ),
        )
        sleeps: list[float] = []

        batch = acquire_api_overnight_volume(
            client,
            ["aaa", "bbb"],
            trade_date=TRADE_DATE,
            request_interval_seconds=0.5,
            now_factory=Clock(),
            sleep=sleeps.append,
        )

        self.assertEqual(
            [item.symbol for item in batch.observations], ["AAA", "BBB"]
        )
        self.assertEqual(
            [item.ov_decision for item in batch.observations], [0, 50]
        )
        self.assertEqual(batch.successful_count, 2)
        self.assertEqual(batch.failed_count, 0)
        self.assertEqual(sleeps, [0.5])
        _, request = client.calls[0]
        self.assertTrue(request["needExtendedHoursData"])
        self.assertFalse(request["needPreviousClose"])
        self.assertEqual(request["startDate"].hour, 0)
        self.assertEqual(
            (request["endDate"].hour, request["endDate"].minute),
            (9, 0),
        )

    def test_explicit_retrospective_window_changes_request_boundary(self) -> None:
        client = FakeClient(
            FakeResponse(payload={"symbol": "AAA", "candles": []})
        )

        acquire_api_overnight_volume(
            client,
            ["AAA"],
            trade_date=TRADE_DATE,
            window_end=time(9, 30),
            request_interval_seconds=0,
            now_factory=Clock(),
            sleep=lambda _: None,
        )

        _, request = client.calls[0]
        self.assertEqual(
            (request["endDate"].hour, request["endDate"].minute),
            (9, 30),
        )

    def test_retries_429_and_preserves_attempt_count(self) -> None:
        client = FakeClient(
            FakeResponse(status_code=429, headers={"Retry-After": "2"}),
            FakeResponse(payload={"symbol": "AAA", "candles": []}),
        )
        sleeps: list[float] = []

        batch = acquire_api_overnight_volume(
            client,
            ["AAA"],
            trade_date=TRADE_DATE,
            request_interval_seconds=0.5,
            now_factory=Clock(),
            sleep=sleeps.append,
        )

        result = batch.observations[0]
        self.assertEqual(result.status, APIOvernightVolumeStatus.OK)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(batch.request_count, 2)
        self.assertEqual(sleeps, [2.0, 0.5])

    def test_terminal_http_failure_is_explicit(self) -> None:
        client = FakeClient(FakeResponse(status_code=400))

        batch = acquire_api_overnight_volume(
            client,
            ["BAD"],
            trade_date=TRADE_DATE,
            request_interval_seconds=0,
            now_factory=Clock(),
            sleep=lambda _: None,
        )

        result = batch.observations[0]
        self.assertEqual(result.status, APIOvernightVolumeStatus.REQUEST_ERROR)
        self.assertEqual(result.http_status, 400)
        self.assertIsNone(result.ov_decision)
        self.assertEqual(batch.failed_count, 1)

    def test_malformed_json_is_explicit(self) -> None:
        client = FakeClient(
            FakeResponse(json_error=ValueError("bad JSON"))
        )

        batch = acquire_api_overnight_volume(
            client,
            ["BAD"],
            trade_date=TRADE_DATE,
            request_interval_seconds=0,
            now_factory=Clock(),
            sleep=lambda _: None,
        )

        result = batch.observations[0]
        self.assertEqual(result.status, APIOvernightVolumeStatus.RESPONSE_ERROR)
        self.assertIn("bad JSON", result.detail or "")


class TestAPIOvernightVolumeArtifacts(unittest.TestCase):
    def test_writes_hashed_immutable_evidence_bundle(self) -> None:
        start = datetime(2026, 9, 21, 13, 5, tzinfo=UTC)
        observation = APIOvernightVolumeObservation(
            symbol="AAA",
            trade_date=TRADE_DATE,
            window_start_et=datetime(2026, 9, 21, 0, 0, tzinfo=ET),
            window_end_et=datetime(2026, 9, 21, 9, 0, tzinfo=ET),
            status=APIOvernightVolumeStatus.OK,
            ov_decision=0,
            candle_count=0,
            attempts=1,
            request_started_at_utc=start,
            response_received_at_utc=start + timedelta(seconds=1),
            http_status=200,
            detail=None,
            candles=(),
        )
        batch = APIOvernightVolumeBatch(
            trade_date=TRADE_DATE,
            window_start_et=observation.window_start_et,
            window_end_et=observation.window_end_et,
            started_at_utc=start,
            completed_at_utc=start + timedelta(seconds=1),
            request_interval_seconds=0.5,
            max_attempts=3,
            observations=(observation,),
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            opening = root / "opening.json"
            opening.write_text("{}\n", encoding="utf-8")
            output = root / "evidence"
            artifacts = write_api_overnight_volume_artifacts(
                output,
                opening_proposal_path=opening,
                opening_content_sha256="a" * 64,
                opening_uni_count=1,
                opening_effective_at=datetime(
                    2026, 9, 21, 9, 30, tzinfo=ET
                ),
                complete_opening_uni=True,
                batch=batch,
            )
            manifest = json.loads(
                artifacts.manifest.read_text(encoding="utf-8")
            )
            with artifacts.observations.open(
                encoding="utf-8", newline=""
            ) as file:
                rows = list(csv.DictReader(file))

            self.assertEqual(manifest["failed_symbols"], 0)
            self.assertEqual(manifest["status_counts"], {"ok": 1})
            self.assertTrue(manifest["complete_opening_uni"])
            self.assertTrue(manifest["started_at_or_after_window_end"])
            self.assertTrue(manifest["production_eligible"])
            self.assertEqual(rows[0]["ov_decision"], "0")

            early_artifacts = write_api_overnight_volume_artifacts(
                root / "early-evidence",
                opening_proposal_path=opening,
                opening_content_sha256="a" * 64,
                opening_uni_count=1,
                opening_effective_at=datetime(
                    2026, 9, 21, 9, 30, tzinfo=ET
                ),
                complete_opening_uni=True,
                batch=replace(
                    batch,
                    started_at_utc=datetime(
                        2026, 9, 21, 12, 59, 59, tzinfo=UTC
                    ),
                ),
            )
            early_manifest = json.loads(
                early_artifacts.manifest.read_text(encoding="utf-8")
            )
            self.assertFalse(
                early_manifest["started_at_or_after_window_end"]
            )
            self.assertFalse(early_manifest["production_eligible"])

            with self.assertRaises(FileExistsError):
                write_api_overnight_volume_artifacts(
                    output,
                    opening_proposal_path=opening,
                    opening_content_sha256="a" * 64,
                    opening_uni_count=1,
                    opening_effective_at=datetime(
                        2026, 9, 21, 9, 30, tzinfo=ET
                    ),
                    complete_opening_uni=True,
                    batch=batch,
                )


if __name__ == "__main__":
    unittest.main()
