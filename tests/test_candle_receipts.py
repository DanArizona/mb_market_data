from __future__ import annotations

import hashlib
import json
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch

from mb_market_data.candle_receipts import CandleReceiptError, CandleReceiptReader
from mb_market_data.observation_overlay_view import (
    build_observation_overlay_figure, resolve_reference, selected_reference_from_click,
)
from mb_market_data.quote_dashboard import create_quote_dashboard
from mb_market_data.quote_dashboard_live import QuoteDashboardLiveController
from mb_market_data.quote_journal_live import QuoteJournalLiveReader
from test_quote_journal_live import LiveFixture, BOUNDARY, SESSION, UTC
from mb_market_data.watchlist_polling import WatchlistKind
from test_live_overlay_dashboard import request_callback


def capture_plan(directory, symbol="TEST"):
    directory.mkdir()
    start = BOUNDARY.replace(hour=13, minute=30)
    end = BOUNDARY.replace(hour=20, minute=0)
    (directory / "request_plan.json").write_text(json.dumps({
        "symbol": symbol, "session_date": str(SESSION), "frequency_minutes": 5,
        "extended_hours": False, "request_start_et": start.isoformat(), "request_end_et": end.isoformat(),
    }))


def write_receipt(directory, number, received, *, close=100, volume=50, outcome="OK", receipt=True):
    value = {"symbol": "TEST", "candles": [{"datetime": int((BOUNDARY-timedelta(minutes=5)).timestamp()*1000),
                "open": 100, "high": 130, "low": 90, "close": close, "volume": volume}]}
    body = json.dumps(value).encode()
    raw_name = f"sample_{number:04d}.response.txt"
    (directory / raw_name).write_bytes(body)
    payload = {"sample_number": number, "dispatched_at_utc": (received-timedelta(seconds=.3)).isoformat(),
               "received_at_utc": received.isoformat(), "outcome": outcome,
               "http_status": 200 if outcome == "OK" else 500,
               "raw_response_file": raw_name, "raw_sha256": hashlib.sha256(body).hexdigest()}
    if receipt:
        (directory / f"sample_{number:04d}.receipt.json").write_text(json.dumps(payload))
    return payload


class CandleTests(LiveFixture):
    def setUp(self):
        super().setUp()
        self.capture = self.path.parent / "capture"
        capture_plan(self.capture)

    def candle_reader(self):
        return CandleReceiptReader(self.capture)

    def with_candles(self, reader=None, limit=32):
        return QuoteDashboardLiveController(reader=QuoteJournalLiveReader(self.path), symbol="TEST",
            now=lambda: self.clock, batch_limit=limit, candle_reader=reader or self.candle_reader())

    def test_single_candle_has_interval_spacing_and_explicit_volume_width(self):
        write_receipt(self.capture, 1, BOUNDARY+timedelta(seconds=3), close=101)
        self.clock = BOUNDARY+timedelta(seconds=20)
        controller = self.with_candles()
        overlay = controller.snapshot().observation_overlay
        figure = build_observation_overlay_figure(overlay)
        candle, volume = figure.data[:2]
        self.assertEqual(len(overlay.candles), 1)
        self.assertEqual(candle.x[0] - candle.x[1], timedelta(minutes=5))
        self.assertEqual(tuple(candle.close), (101, None))
        self.assertIsNone(candle.customdata[1])
        self.assertEqual(tuple(volume.y), (50,))
        self.assertEqual(volume.width, 240000)
        self.assertIsNone(selected_reference_from_click(overlay, {"points": [{"curveNumber": 0, "pointNumber": 1}]}, figure.to_plotly_json(), "close"))

    def test_candle_only_step_updates_response_counter_and_progress(self):
        from mb_market_data.quote_dashboard import _control_values
        write_receipt(self.capture, 1, BOUNDARY+timedelta(seconds=3), close=101)
        write_receipt(self.capture, 2, BOUNDARY+timedelta(seconds=25), close=102)
        self.clock = BOUNDARY+timedelta(seconds=30)
        controller = self.with_candles()
        before = controller.seek(BOUNDARY)
        after = controller.step()
        self.assertEqual(after.applied_event_count, before.applied_event_count)
        self.assertEqual(after.total_candle_receipt_count, 2)
        self.assertGreater(after.progress_fraction, before.progress_fraction)
        self.assertIn("journal events (membership/quotes)", _control_values(after)[7])
        self.assertIn("1 / 2 candle responses", _control_values(after)[7])

    def test_partial_does_not_become_closed_by_clock_alone(self):
        write_receipt(self.capture, 1, BOUNDARY-timedelta(seconds=10))
        reader = self.candle_reader()
        reader.refresh()
        view = reader.project(BOUNDARY+timedelta(seconds=20))
        self.assertEqual(view.closed, ())
        self.assertEqual(len(view.pending), 1)
        write_receipt(self.capture, 2, BOUNDARY+timedelta(seconds=30))
        reader.refresh()
        self.assertEqual(reader.project(BOUNDARY+timedelta(seconds=29)).closed, ())
        closed = reader.project(BOUNDARY+timedelta(seconds=30)).closed
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].observed_version, 1)  # same values, new closing receipt
        self.assertEqual(closed[0].received_at_utc, BOUNDARY+timedelta(seconds=30))

    def test_correction_uses_its_own_receipt_and_backward_seek_restores_values(self):
        write_receipt(self.capture, 1, BOUNDARY+timedelta(seconds=10))
        write_receipt(self.capture, 2, BOUNDARY+timedelta(seconds=60), close=110, volume=60)
        reader = self.candle_reader()
        reader.refresh()
        self.assertEqual(reader.project(BOUNDARY+timedelta(seconds=59)).closed[0].candle.close, 100)
        self.assertEqual(reader.project(BOUNDARY+timedelta(seconds=60)).closed[0].candle.close, 110)
        self.clock = BOUNDARY+timedelta(seconds=120)
        controller = self.with_candles(reader)
        self.assertEqual(controller.snapshot().observation_overlay.candles[0].close, 110)
        old = controller.seek(BOUNDARY+timedelta(seconds=10)).observation_overlay
        self.assertEqual(old.candles[0].close, 100)
        self.assertEqual(old.captured_candle_versions[0].observed_version, 1)
        self.assertEqual(controller.play().observation_overlay.candles[0].close, 110)

    def test_new_reader_matches_bounded_reader_at_every_cutoff(self):
        for number, seconds in enumerate((-10, 10, 30), 1):
            write_receipt(self.capture, number, BOUNDARY+timedelta(seconds=seconds), close=99+number)
        reader, fresh = self.candle_reader(), self.candle_reader()
        for _ in range(3): reader.refresh(limit=1)
        fresh.refresh()
        for seconds in (-20, -10, 0, 10, 29, 30):
            cutoff = BOUNDARY+timedelta(seconds=seconds)
            self.assertEqual(reader.project(cutoff), fresh.project(cutoff))

    def test_raw_file_without_receipt_is_not_visible(self):
        payload = write_receipt(self.capture, 1, BOUNDARY, receipt=False)
        reader = self.candle_reader()
        reader.refresh()
        self.assertEqual(reader.project(BOUNDARY).receipt_count, 0)
        (self.capture / "sample_0001.receipt.json").write_text(json.dumps(payload))
        reader.refresh()
        self.assertEqual(reader.project(BOUNDARY).receipt_count, 1)

    def test_incomplete_receipt_is_retried_without_advancing_state(self):
        payload = write_receipt(self.capture, 1, BOUNDARY, receipt=False)
        path = self.capture / "sample_0001.receipt.json"
        path.write_text("{")
        reader = self.candle_reader()
        with self.assertRaises(ValueError): reader.refresh()
        self.assertEqual(reader.project(BOUNDARY).receipt_count, 0)
        path.write_text(json.dumps(payload))
        reader.refresh()
        self.assertEqual(reader.project(BOUNDARY).receipt_count, 1)

    def test_changed_plan_and_extreme_timestamp_fail_visibly(self):
        payload = write_receipt(self.capture, 1, BOUNDARY)
        raw = json.loads((self.capture / payload["raw_response_file"]).read_text())
        raw["candles"][0]["datetime"] = 1e300
        body = json.dumps(raw).encode()
        (self.capture / payload["raw_response_file"]).write_bytes(body)
        payload["raw_sha256"] = hashlib.sha256(body).hexdigest()
        (self.capture / "sample_0001.receipt.json").write_text(json.dumps(payload))
        reader = self.candle_reader()
        with self.assertRaises(CandleReceiptError): reader.refresh()
        (self.capture / "request_plan.json").write_text("{}")
        with self.assertRaises(CandleReceiptError): reader.refresh()

    def test_conflicting_batch_is_not_partially_applied(self):
        write_receipt(self.capture, 1, BOUNDARY)
        reader = self.candle_reader()
        reader.refresh()
        original = reader.project(BOUNDARY+timedelta(seconds=20))
        write_receipt(self.capture, 2, BOUNDARY+timedelta(seconds=10))
        payload = write_receipt(self.capture, 3, BOUNDARY+timedelta(seconds=20))
        (self.capture / payload["raw_response_file"]).write_text("changed")
        with self.assertRaises(CandleReceiptError): reader.refresh()
        self.assertEqual(reader.project(BOUNDARY+timedelta(seconds=20)), original)

    def test_changed_or_removed_discovered_evidence_is_refused(self):
        write_receipt(self.capture, 1, BOUNDARY)
        reader = self.candle_reader()
        reader.refresh()
        raw = self.capture / "sample_0001.response.txt"
        raw.write_bytes(raw.read_bytes()+b" ")
        with self.assertRaises(CandleReceiptError): reader.refresh()
        raw.unlink()
        with self.assertRaises(OSError): reader.refresh()

    def test_errors_are_counted_at_receipt_without_replacing_good_candles(self):
        write_receipt(self.capture, 1, BOUNDARY)
        write_receipt(self.capture, 2, BOUNDARY+timedelta(seconds=10), outcome="HTTP_ERROR")
        reader = self.candle_reader()
        reader.refresh()
        self.assertEqual(reader.project(BOUNDARY).error_count, 0)
        view = reader.project(BOUNDARY+timedelta(seconds=10))
        self.assertEqual(view.error_count, 1)
        self.assertEqual(view.closed[0].sample_number, 1)

    def test_capture_identity_and_raw_path_are_guarded(self):
        with self.assertRaises(ValueError):
            QuoteDashboardLiveController(reader=QuoteJournalLiveReader(self.path), symbol="OTHER",
                                         candle_reader=self.candle_reader(), now=lambda:self.clock)
        payload = write_receipt(self.capture, 1, BOUNDARY)
        payload["raw_response_file"] = "../request_plan.json"
        (self.capture / "sample_0001.receipt.json").write_text(json.dumps(payload))
        with self.assertRaises(CandleReceiptError): self.candle_reader().refresh()

    def test_pause_keeps_discovery_receipt_cutoff_and_step_advances_candles(self):
        write_receipt(self.capture, 1, BOUNDARY)
        controller = self.with_candles()
        controller.pause()
        write_receipt(self.capture, 2, BOUNDARY+timedelta(seconds=10), close=110)
        self.clock = BOUNDARY+timedelta(seconds=20)
        controller.tick()
        self.assertEqual(controller.snapshot().observation_overlay.candles[0].close, 100)
        self.assertEqual(controller.step().observation_overlay.candles[0].close, 110)

    def test_candle_read_error_does_not_stop_quote_discovery(self):
        write_receipt(self.capture, 1, BOUNDARY)
        controller = self.with_candles()
        self.acquire()
        self.clock = BOUNDARY+timedelta(seconds=10)
        (self.capture / "sample_0001.response.txt").write_text("damaged")
        snapshot = controller.tick()
        self.assertIn("Candle source:", snapshot.source_error)
        self.assertEqual(snapshot.state.acquisition_count, 1)
        self.assertEqual(snapshot.observation_overlay.candles[0].close, 100)
        self.assertEqual(controller.seek(BOUNDARY-timedelta(seconds=1)).observation_overlay.candles, ())

    def test_hover_and_percent_reference_use_received_time(self):
        write_receipt(self.capture, 1, BOUNDARY+timedelta(seconds=10))
        self.clock = BOUNDARY+timedelta(seconds=20)
        overlay = self.with_candles().snapshot().observation_overlay
        figure = build_observation_overlay_figure(overlay).to_plotly_json()
        self.assertEqual(figure["data"][0]["customdata"][0][10], (BOUNDARY+timedelta(seconds=10)).isoformat())
        selection = selected_reference_from_click(overlay, {"points":[{"curveNumber":0,"pointNumber":0}]}, figure, "close")
        self.assertEqual(selection["available_at_utc"], (BOUNDARY+timedelta(seconds=10)).isoformat())
        from dataclasses import replace
        older = replace(overlay, replay_time_utc=BOUNDARY, candles=(), captured_candle_versions=())
        self.assertIsNone(resolve_reference(older, selection))

    def test_click_on_older_render_keeps_that_versions_price_and_receipt(self):
        write_receipt(self.capture, 1, BOUNDARY+timedelta(seconds=10))
        self.clock = BOUNDARY+timedelta(seconds=20)
        controller = self.with_candles()
        earlier = build_observation_overlay_figure(controller.snapshot().observation_overlay).to_plotly_json()
        write_receipt(self.capture, 2, BOUNDARY+timedelta(seconds=30), close=110)
        self.clock = BOUNDARY+timedelta(seconds=40)
        latest = controller.tick().observation_overlay
        selected = selected_reference_from_click(latest, {"points":[{"curveNumber":0,"pointNumber":0}]}, earlier, "close")
        self.assertEqual(selected["price"], 100)
        self.assertEqual(selected["available_at_utc"], (BOUNDARY+timedelta(seconds=10)).isoformat())

    def test_real_dash_callback_shows_receipt_counts_and_candles(self):
        write_receipt(self.capture, 1, BOUNDARY)
        controller = self.with_candles()
        app = create_quote_dashboard(controller)
        client = app.server.test_client()
        layout = client.get("/_dash-layout")
        self.assertIn(b"unfinished candles are held back", layout.data)
        values = {("replay-clock", "children"):"clock", ("theme-preference","data"):"light",
                  ("observation-overlay-units","value"):"price"}
        response = request_callback(client, app, "observation-overlay-units", "replay-clock.children", values)
        self.assertEqual(response.status_code, 200)
        result = response.get_json()["response"]
        self.assertIn("captured responses", result["observation-overlay-evidence"]["children"])
        self.assertEqual(result["observation-overlay-chart"]["figure"]["data"][0]["name"], "5-minute OHLC")

    def test_display_cutoff_updates_when_step_and_seek_add_no_quote_events(self):
        self.publish(2, 1, ("OTHER", "TEST"))
        self.acquire(completed=2.47)
        self.acquire(WatchlistKind.FOCUS, scheduled=5, completed=5.3, revision=2)
        write_receipt(self.capture, 1, BOUNDARY-timedelta(seconds=10), close=101.5, volume=1000)
        write_receipt(self.capture, 2, BOUNDARY+timedelta(seconds=3), close=102, volume=1300)
        write_receipt(self.capture, 3, BOUNDARY+timedelta(seconds=50), close=101.8, volume=1320)
        self.clock = BOUNDARY+timedelta(seconds=60)
        controller = self.with_candles()
        before = controller.seek(BOUNDARY+timedelta(seconds=2, microseconds=999999))
        self.assertEqual(before.observation_overlay.candles, ())
        app = create_quote_dashboard(controller)
        client = app.server.test_client()
        values = {("replay-speed", "value"): 1,
                  ("rendered-event-count", "data"): before.applied_event_count}

        # The next event is a candle receipt, not another journal event.
        stepped = request_callback(client, app, "replay-toggle", "replay-step.n_clicks", values)
        self.assertEqual(stepped.status_code, 200)
        result = stepped.get_json()["response"]
        self.assertEqual(result["replay-clock"]["children"], "2026-10-06 13:30:03.000 ET")
        self.assertEqual(result["state-as-of"]["children"], "Viewing time: 2026-10-06 13:30:03.000 ET")
        self.assertNotIn("market-state-grid", result)
        self.assertEqual(controller.snapshot().applied_event_count, before.applied_event_count)
        self.assertEqual(controller.snapshot().observation_overlay.candles[0].close, 102)

        # Seeking between receipts updates the time even with unchanged data.
        values[("replay-seek-time", "value")] = "13:30:04"
        sought = request_callback(client, app, "replay-toggle", "replay-seek.n_clicks", values)
        self.assertEqual(sought.status_code, 200)
        result = sought.get_json()["response"]
        self.assertEqual(result["state-as-of"]["children"], "Viewing time: 2026-10-06 13:30:04.999 ET")
        self.assertEqual(controller.snapshot().observation_overlay.candles[0].close, 102)

        # A further Step reaches the Focus response at 13:30:05.300.
        focused = request_callback(client, app, "replay-toggle", "replay-step.n_clicks", values)
        self.assertEqual(focused.status_code, 200)
        self.assertEqual(focused.get_json()["response"]["replay-clock"]["children"],
                         "2026-10-06 13:30:05.300 ET")

        # The correction is visible only at its own receipt cutoff.
        for seek_time, expected_close in (("13:30:49", 102), ("13:30:51", 101.8)):
            values[("replay-seek-time", "value")] = seek_time
            response = request_callback(client, app, "replay-toggle", "replay-seek.n_clicks", values)
            self.assertEqual(response.status_code, 200)
            overlay = controller.snapshot().observation_overlay
            self.assertEqual(overlay.candles[0].close, expected_close)
            chart_response = request_callback(client, app, "observation-overlay-units", "replay-clock.children", {
                ("replay-clock", "children"): response.get_json()["response"]["replay-clock"]["children"],
                ("theme-preference", "data"): "dark", ("observation-overlay-units", "value"): "price",
            })
            self.assertEqual(chart_response.status_code, 200)
            chart = chart_response.get_json()["response"]
            self.assertEqual(chart["observation-overlay-as-of"]["children"],
                             response.get_json()["response"]["state-as-of"]["children"])
            self.assertEqual(chart["observation-overlay-chart"]["figure"]["data"][0]["close"], [expected_close, None])
