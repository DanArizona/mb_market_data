from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from mb_market_data.observation_overlay import ET, load_observation_overlay_cache
from mb_market_data.observation_overlay_acquisition import (
    ObservationOverlayDataError,
    ObservationOverlayRequestError,
    acquire_observation_overlay_ohlcv,
    parse_observation_overlay_payload,
    validate_completed_session,
    write_observation_overlay_acquisition,
)


UTC = timezone.utc
SESSION_DATE = date(2026, 9, 24)


def epoch_ms(hour: int, minute: int) -> int:
    value = datetime(2026, 9, 24, hour, minute, tzinfo=ET)
    return int(value.timestamp() * 1000)


def raw_candle(hour: int, minute: int, volume: int = 100) -> dict[str, object]:
    return {
        "datetime": epoch_ms(hour, minute),
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.5,
        "volume": volume,
    }


class FakeResponse:
    def __init__(
        self,
        payload: object,
        *,
        ok: bool = True,
        status_code: int = 200,
        content: bytes | None = None,
    ) -> None:
        self._payload = payload
        self.ok = ok
        self.status_code = status_code
        self.content = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if content is None
            else content
        )

    def json(self) -> object:
        return self._payload


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def price_history(self, *args: object, **kwargs: object) -> FakeResponse:
        self.calls.append((args, kwargs))
        return self.response


def clock() -> object:
    values = iter(
        (
            datetime(2026, 9, 24, 20, 1, 0, tzinfo=UTC),
            datetime(2026, 9, 24, 20, 1, 1, tzinfo=UTC),
        )
    )
    return lambda: next(values)


class TestObservationOverlayAcquisition(unittest.TestCase):
    def test_acquires_exact_raw_payload_and_normalizes_session(self) -> None:
        payload = {
            "symbol": "test",
            "candles": [
                raw_candle(16, 0, 999),
                raw_candle(9, 35, 200),
                raw_candle(9, 30, 100),
            ],
        }
        response = FakeResponse(payload)
        client = FakeClient(response)

        result = acquire_observation_overlay_ohlcv(
            client,
            symbol=" test ",
            session_date=SESSION_DATE,
            now_factory=clock(),  # type: ignore[arg-type]
        )

        self.assertEqual(result.raw_payload, response.content)
        self.assertEqual(
            result.cache.source_payload_sha256,
            hashlib.sha256(response.content).hexdigest(),
        )
        self.assertEqual(result.cache.symbol, "TEST")
        self.assertEqual(
            tuple(candle.start_et.strftime("%H:%M") for candle in result.cache.candles),
            ("09:30", "09:35"),
        )
        self.assertEqual(result.http_status, 200)
        self.assertEqual(len(client.calls), 1)
        args, kwargs = client.calls[0]
        self.assertEqual(args, ("TEST",))
        self.assertEqual(kwargs["frequencyType"], "minute")
        self.assertEqual(kwargs["frequency"], 5)
        start_date = kwargs["startDate"]
        end_date = kwargs["endDate"]
        self.assertIsInstance(start_date, datetime)
        self.assertIsInstance(end_date, datetime)
        self.assertEqual(start_date.astimezone(ET).strftime("%H:%M"), "00:00")
        self.assertEqual(end_date.astimezone(ET).strftime("%H:%M"), "16:00")
        self.assertIs(kwargs["needExtendedHoursData"], True)
        self.assertIs(kwargs["needPreviousClose"], False)

    def test_rejects_http_failure_and_invalid_json(self) -> None:
        failure = FakeClient(FakeResponse({}, ok=False, status_code=503))
        with self.assertRaisesRegex(ObservationOverlayRequestError, "HTTP 503"):
            acquire_observation_overlay_ohlcv(
                failure,
                symbol="TEST",
                session_date=SESSION_DATE,
                now_factory=clock(),  # type: ignore[arg-type]
            )

        invalid = FakeClient(FakeResponse({}, content=b"not-json"))
        with self.assertRaisesRegex(ObservationOverlayRequestError, "not valid JSON"):
            acquire_observation_overlay_ohlcv(
                invalid,
                symbol="TEST",
                session_date=SESSION_DATE,
                now_factory=clock(),  # type: ignore[arg-type]
            )

    def test_rejects_symbol_mismatch_and_duplicate_candle(self) -> None:
        common = {
            "symbol": "OTHER",
            "candles": [raw_candle(9, 30)],
        }
        with self.assertRaisesRegex(ObservationOverlayDataError, "symbol mismatch"):
            parse_observation_overlay_payload(
                common,
                symbol="TEST",
                session_date=SESSION_DATE,
                acquired_at_utc=datetime(2026, 9, 24, 20, 0, tzinfo=UTC),
                source_payload_sha256="a" * 64,
            )

        common["symbol"] = "TEST"
        common["candles"] = [raw_candle(9, 30), raw_candle(9, 30)]
        with self.assertRaisesRegex(ObservationOverlayDataError, "duplicate"):
            parse_observation_overlay_payload(
                common,
                symbol="TEST",
                session_date=SESSION_DATE,
                acquired_at_utc=datetime(2026, 9, 24, 20, 0, tzinfo=UTC),
                source_payload_sha256="a" * 64,
            )

    def test_completed_session_guard(self) -> None:
        with self.assertRaisesRegex(ValueError, "completed session"):
            validate_completed_session(
                SESSION_DATE,
                datetime(2026, 9, 24, 15, 59, 59, tzinfo=ET),
            )
        validate_completed_session(
            SESSION_DATE,
            datetime(2026, 9, 24, 16, 0, tzinfo=ET),
        )

    def test_writes_verifiable_immutable_evidence_bundle(self) -> None:
        payload = {"symbol": "TEST", "candles": [raw_candle(9, 30)]}
        response = FakeResponse(payload)
        acquisition = acquire_observation_overlay_ohlcv(
            FakeClient(response),
            symbol="TEST",
            session_date=SESSION_DATE,
            now_factory=clock(),  # type: ignore[arg-type]
        )

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "bundle"
            artifacts = write_observation_overlay_acquisition(
                output_dir,
                acquisition,
            )

            self.assertEqual(artifacts.raw_payload.read_bytes(), response.content)
            loaded = load_observation_overlay_cache(artifacts.cache)
            self.assertEqual(loaded, acquisition.cache)
            manifest = json.loads(artifacts.manifest.read_text(encoding="utf-8"))
            self.assertEqual(manifest["acquisition_version"], "observation-overlay-acquisition-v1")
            self.assertEqual(manifest["candle_count"], 1)
            self.assertEqual(
                manifest["artifacts"]["raw_payload"]["sha256"],
                acquisition.cache.source_payload_sha256,
            )
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                write_observation_overlay_acquisition(output_dir, acquisition)

    def test_rejects_provenance_mismatch_before_creating_directory(self) -> None:
        payload = {"symbol": "TEST", "candles": [raw_candle(9, 30)]}
        acquisition = acquire_observation_overlay_ohlcv(
            FakeClient(FakeResponse(payload)),
            symbol="TEST",
            session_date=SESSION_DATE,
            now_factory=clock(),  # type: ignore[arg-type]
        )
        corrupted = replace(acquisition, raw_payload=b"different")

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "bundle"
            with self.assertRaisesRegex(ObservationOverlayDataError, "provenance"):
                write_observation_overlay_acquisition(output_dir, corrupted)
            self.assertFalse(output_dir.exists())


if __name__ == "__main__":
    unittest.main()
