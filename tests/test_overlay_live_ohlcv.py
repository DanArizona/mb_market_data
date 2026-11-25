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

    def controller(self):
        from mb_market_data.observation_overlay import ObservationOverlayProjector
        from mb_market_data.quote_dashboard_replay import QuoteDashboardReplayController
        from mb_market_data.quote_journal_replay import MembershipRevisionEvent, ReplayTimeline
        from mb_market_data.sampling_membership import SamplingHierarchyRevision

        at = instant(9, 30).astimezone(UTC)
        event = MembershipRevisionEvent(SamplingHierarchyRevision(
            session_date=DAY, revision=0, effective_at=at,
            uni_symbols=("TEST", "AAPL"), focus_symbols=("TEST",),
            hot_symbols=(), source="offline-live-candle-test",
        ))
        controller = QuoteDashboardReplayController(
            timeline=ReplayTimeline(DAY, 1, at, at),
            event_factory=lambda: iter((event,)),
            overlay_projector_factory=lambda: ObservationOverlayProjector(
                cache=None, symbol="TEST", session_date=DAY,
            ),
            symbol_projector_factory=lambda symbol: ObservationOverlayProjector(
                cache=None, symbol=symbol, session_date=DAY,
            ),
        )
        controller.finish()
        return controller

    def test_live_controller_accepts_closed_bars_without_journal_mutation(self):
        controller = self.controller()
        before = controller.snapshot()
        cache = acquisition(StubClient(self.payload), 9, 37).cache
        with self.assertRaisesRegex(RuntimeError, "Real Time"):
            controller.install_live_ohlcv_cache(cache)
        controller.activate_live_readonly()
        self.assertTrue(controller.install_live_ohlcv_cache(cache))
        self.assertFalse(controller.install_live_ohlcv_cache(cache))
        after = controller.snapshot()
        self.assertEqual(after.applied_event_count, before.applied_event_count)
        self.assertEqual(after.state.revision_count, before.state.revision_count)
        self.assertEqual(after.replay_time_utc, before.replay_time_utc)
        self.assertEqual([c.volume for c in after.observation_overlay.candles], [111])
        self.assertEqual(after.observation_overlay.symbol, "TEST")
        self.assertEqual(after.observation_overlay.replay_time_utc, cache.acquired_at_utc)

        extended = acquisition(StubClient(self.payload), 9, 40).cache
        self.assertTrue(controller.install_live_ohlcv_cache(extended))
        self.assertEqual(
            [c.volume for c in controller.snapshot().observation_overlay.candles],
            [111, 222],
        )
        changed_first = replace(extended.candles[0], close=103.0)
        tampered_later = replace(
            extended, acquired_at_utc=extended.acquired_at_utc.replace(second=3),
            candles=(changed_first, extended.candles[1]),
        )
        with self.assertRaisesRegex(ValueError, "changed or removed"):
            controller.install_live_ohlcv_cache(tampered_later)
        with self.assertRaisesRegex(ValueError, "another selected symbol"):
            controller.select_overlay_symbol("AAPL")
            controller.install_live_ohlcv_cache(extended)

    def test_live_controller_rejects_same_time_conflict_and_wrong_session(self):
        controller = self.controller()
        controller.activate_live_readonly()
        cache = acquisition(StubClient(self.payload), 9, 37).cache
        self.assertTrue(controller.install_live_ohlcv_cache(cache))
        # Wrong-day evidence is rejected even at cache construction.
        with self.assertRaisesRegex(ValueError, "candle is not on cache"):
            replace(cache, session_date=date(2026, 11, 24))
        with self.assertRaisesRegex(ValueError, "cache symbol"):
            from mb_market_data.observation_overlay import ObservationOverlayProjector
            projector = ObservationOverlayProjector(
                cache=cache, symbol="AAPL", session_date=DAY,
            )
        altered = replace(
            cache,
            candles=(replace(cache.candles[0], close=103.0),),
        )
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            controller.install_live_ohlcv_cache(altered)
        self.assertEqual(
            [c.volume for c in controller.snapshot().observation_overlay.candles],
            [111],
        )

    def test_selected_symbol_switch_does_not_reuse_previous_candles(self):
        controller = self.controller()
        controller.activate_live_readonly()
        cache = acquisition(StubClient(self.payload), 9, 37).cache
        self.assertTrue(controller.install_live_ohlcv_cache(cache))
        switched = controller.select_overlay_symbol("AAPL")
        self.assertEqual(switched.observation_overlay.symbol, "AAPL")
        self.assertEqual(switched.observation_overlay.candles, ())
        with self.assertRaisesRegex(ValueError, "another selected symbol"):
            controller.install_live_ohlcv_cache(cache)

    def test_live_reader_detects_new_publication_for_selected_symbol(self):
        controller = self.controller()
        controller.activate_live_readonly()
        with tempfile.TemporaryDirectory() as tmp:
            reader = LiveOHLCVReader(tmp, session_date=DAY)
            now = instant(9, 40, 3).astimezone(UTC)
            self.assertIsNone(reader.latest("TEST", now=now))
            first = acquisition(StubClient(self.payload), 9, 37)
            publish_live_ohlcv(tmp, first)
            self.assertTrue(controller.install_live_ohlcv_cache(reader.latest("TEST", now=now)))
            second = acquisition(StubClient(self.payload), 9, 40)
            publish_live_ohlcv(tmp, second)
            self.assertTrue(controller.install_live_ohlcv_cache(reader.latest("TEST", now=now)))
            self.assertEqual(len(controller.snapshot().observation_overlay.candles), 2)
            self.assertEqual(controller.snapshot().applied_event_count, 1)

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
