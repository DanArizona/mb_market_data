"""Offline only: one-client-injection live candles and immutable read-only feed."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from mb_market_data.observation_overlay import ET, OHLCV_INTERVAL
from mb_market_data.overlay_live_ohlcv import (
    LiveOHLCVError, LiveOHLCVNotReady, LiveOHLCVReader,
    acquire_live_ohlcv, publish_live_ohlcv,
)

DAY = date(2026, 11, 25)
UTC = timezone.utc


def instant(hour, minute, second=0):
    return datetime(2026, 11, 25, hour, minute, second, tzinfo=ET)


def raw_candle(hour, minute, volume, *, symbol="TEST"):
    return {
        "datetime": int(instant(hour, minute).timestamp() * 1000),
        "open": 100.0, "high": 105.0, "low": 99.0,
        "close": 104.0, "volume": volume,
    }


class Response:
    ok = True
    status_code = 200

    def __init__(self, payload):
        self.content = json.dumps(payload, sort_keys=True).encode("utf-8")

    def json(self):
        return json.loads(self.content)


class StubClient:
    def __init__(self, payload):
        self.response = Response(payload)
        self.requests = []

    def price_history(self, *args, **kwargs):
        self.requests.append((args, kwargs))
        return self.response


def acquisition(client, hour, minute, second=0):
    start = instant(hour, minute, second).astimezone(UTC)
    after = instant(hour, minute, second + 2).astimezone(UTC)
    clock = iter((start, after))
    return acquire_live_ohlcv(
        client, symbol="TEST", session_date=DAY, now_factory=lambda: next(clock),
    )


class TestLiveOHLCVProducerAndConsumer(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "symbol": "TEST",
            "candles": [
                raw_candle(9, 35, 222),  # provisional before 09:40
                raw_candle(9, 30, 111),  # only completed at 09:37
            ],
        }

    def test_incomplete_candle_is_never_retained(self):
        client = StubClient(self.payload)
        result = acquisition(client, 9, 37)
        self.assertEqual(
            [c.start_et.strftime("%H:%M") for c in result.cache.candles],
            ["09:30"],
        )
        self.assertEqual([c.volume for c in result.cache.candles], [111])
        self.assertEqual(result.skipped_provisional_candles, 1)
        self.assertEqual(result.cache.request_end_et, instant(9, 37))
        self.assertEqual(result.cache.acquired_at_utc, instant(9, 37, 2).astimezone(UTC))
        self.assertEqual(client.requests[0][0], ("TEST",))
        self.assertEqual(client.requests[0][1]["frequency"], 5)
        self.assertEqual(client.requests[0][1]["endDate"], instant(9, 37))
        self.assertTrue(client.requests[0][1]["needExtendedHoursData"])

    def test_no_completed_bar_is_not_ready_and_never_fabricated(self):
        client = StubClient(self.payload)
        with self.assertRaisesRegex(LiveOHLCVNotReady, "No completed"):
            acquisition(client, 9, 33)
        self.assertEqual(len(client.requests), 1)

    def test_newly_closed_bar_appears_at_next_request_boundary(self):
        client = StubClient(self.payload)
        result = acquisition(client, 9, 40)
        self.assertEqual([c.volume for c in result.cache.candles], [111, 222])
        self.assertEqual(result.skipped_provisional_candles, 0)

    def test_rejects_mismatched_day_without_calling_provider(self):
        client = StubClient(self.payload)
        with self.assertRaisesRegex(LiveOHLCVError, "exact Eastern session"):
            acquire_live_ohlcv(
                client, symbol="TEST", session_date=date(2026, 11, 24),
                now_factory=lambda: instant(9, 40).astimezone(UTC),
            )
        self.assertEqual(client.requests, [])

    def test_immutable_bundle_roundtrip_and_symbol_isolation(self):
        client = StubClient(self.payload)
        result = acquisition(client, 9, 37)
        with tempfile.TemporaryDirectory() as tmp:
            bundle = publish_live_ohlcv(tmp, result)
            self.assertTrue((bundle / "price_history_raw.json").is_file())
            self.assertTrue((bundle / "ohlcv_cache.json").is_file())
            self.assertNotIn(".pending-", bundle.name)
            source = LiveOHLCVReader(tmp, session_date=DAY)
            loaded = source.latest("TEST", now=instant(9, 40).astimezone(UTC))
            self.assertEqual(loaded, result.cache)
            self.assertIsNone(source.latest("AAPL", now=instant(9, 40).astimezone(UTC)))
            with self.assertRaises(FileExistsError):
                publish_live_ohlcv(tmp, result)
            with self.assertRaisesRegex(LiveOHLCVError, "exact Eastern session"):
                source.latest("TEST", now=instant(9, 40).astimezone(UTC)
                              .replace(day=26))

    def test_rejects_tampered_cache_and_raw_payload(self):
        client = StubClient(self.payload)
        result = acquisition(client, 9, 37)
        with tempfile.TemporaryDirectory() as tmp:
            bundle = publish_live_ohlcv(tmp, result)
            cache_file = bundle / "ohlcv_cache.json"
            saved = cache_file.read_text(encoding="utf-8")
            corrupted = json.loads(saved)
            corrupted["candles"][0]["volume"] = 99999
            cache_file.write_text(json.dumps(corrupted), encoding="utf-8")
            with self.assertRaisesRegex(LiveOHLCVError, "does not match raw"):
                LiveOHLCVReader(tmp, session_date=DAY).latest(
                    "TEST", now=instant(9, 40).astimezone(UTC),
                )
            cache_file.write_text(saved, encoding="utf-8")
            (bundle / "price_history_raw.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(LiveOHLCVError, "provenance/identity"):
                LiveOHLCVReader(tmp, session_date=DAY).latest(
                    "TEST", now=instant(9, 40).astimezone(UTC),
                )

    def test_publisher_rejects_modified_candle_before_writing(self):
        client = StubClient(self.payload)
        result = acquisition(client, 9, 37)
        changed = replace(result.cache, candles=())
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(LiveOHLCVError, "differs from raw"):
                publish_live_ohlcv(tmp, replace(result, cache=changed))
            self.assertFalse((Path(tmp) / str(DAY)).exists())

    def test_cache_keeps_historical_uniform_blue_volume_style(self):
        # The live path uses the existing figure builder, never an alternate
        # green/red histogram or a separate synthetic candle renderer.
        from mb_market_data.observation_overlay import ObservationOverlayData
        from mb_market_data.observation_overlay_view import build_observation_overlay_figure
        client = StubClient(self.payload)
        cache = acquisition(client, 9, 37).cache
        data = ObservationOverlayData(
            DAY, "TEST", cache.acquired_at_utc, cache, cache.candles, (), (),
        )
        chart = build_observation_overlay_figure(data, theme="dark")
        bars = [trace for trace in chart.data if trace.name == "Volume"]
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].marker.color, "#6fc8ff")
        self.assertEqual(list(bars[0].y), [111])
        self.assertEqual(OHLCV_INTERVAL.total_seconds(), 300)


if __name__ == "__main__":
    unittest.main()
