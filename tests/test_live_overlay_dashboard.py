from __future__ import annotations

import runpy
import sys
from pathlib import Path
from unittest.mock import patch

from mb_market_data.quote_dashboard import create_quote_dashboard
from mb_market_data.observation_overlay_view import build_observation_overlay_figure
from test_quote_journal_live import LiveFixture, BOUNDARY
from datetime import timedelta


def request_callback(client, app, input_id, changed, values):
    key, callback = next(
        (key, callback) for key, callback in app.callback_map.items()
        if any(item["id"] == input_id for item in callback["inputs"])
    )
    def items(definitions):
        return [dict(item, value=values.get((item["id"], item["property"])))
                for item in definitions]
    response = client.post("/_dash-update-component", json={
        "output": key,
        "outputs": [{"id": o.component_id, "property": o.component_property}
                    for o in callback["output"]],
        "inputs": items(callback["inputs"]), "state": items(callback["state"]),
        "changedPropIds": [changed],
    })
    return response


class TestLiveDashboard(LiveFixture):
    def test_empty_cache_layout_and_quote_chart_render(self):
        controller = self.controller()
        app = create_quote_dashboard(controller, source_label="Synthetic data · simulated clock")
        client = app.server.test_client()
        layout = client.get("/_dash-layout")
        self.assertEqual(layout.status_code, 200)
        self.assertIn(b"Candle acquisition is pending", layout.data)
        self.assertIn(b"Synthetic data", layout.data)
        self.assertIn(b"observation-overlay-chart", layout.data)
        self.assertIsNone(controller.snapshot().observation_overlay.cache)
        self.acquire()
        self.clock = BOUNDARY + timedelta(seconds=10)
        snapshot = self.drain(controller)
        figure = build_observation_overlay_figure(snapshot.observation_overlay)
        self.assertTrue(figure["data"])

    def test_tick_pause_and_return_live_callbacks_keep_refresh_enabled(self):
        controller = self.controller()
        app = create_quote_dashboard(controller)
        client = app.server.test_client()
        values = {("replay-speed", "value"): 1,
                  ("rendered-event-count", "data"): controller.snapshot().applied_event_count,
                  ("replay-tick", "n_intervals"): 1}
        paused = request_callback(client, app, "replay-toggle", "replay-toggle.n_clicks", values)
        self.assertEqual(paused.status_code, 200)
        data = paused.get_json()["response"]
        self.assertEqual(data["replay-toggle"]["children"], "Return to Live")
        self.assertFalse(data["replay-tick"]["disabled"])
        self.acquire()
        self.clock = BOUNDARY + timedelta(seconds=10)
        refreshed = request_callback(client, app, "replay-toggle", "replay-tick.n_intervals", values)
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(controller.snapshot().state.acquisition_count, 0)
        returned = request_callback(client, app, "replay-toggle", "replay-toggle.n_clicks", values)
        self.assertEqual(returned.status_code, 200)
        self.assertEqual(returned.get_json()["response"]["replay-toggle"]["children"], "Pause")
        self.assertEqual(controller.snapshot().state.acquisition_count, 1)

    def test_overlay_callback_renders_without_candle_cache(self):
        controller = self.controller()
        self.acquire()
        self.clock = BOUNDARY + timedelta(seconds=10)
        self.drain(controller)
        app = create_quote_dashboard(controller)
        client = app.server.test_client()
        values = {("replay-clock", "children"): "clock", ("theme-preference", "data"): "dark",
                  ("observation-overlay-units", "value"): "price"}
        for channel in ("uni", "focus", "hot"):
            values[(f"observation-overlay-{channel}-size", "value")] = "standard"
            values[(f"observation-overlay-{channel}-opacity", "value")] = 100
        response = request_callback(client, app, "observation-overlay-units",
                                    "replay-clock.children", values)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["response"]["observation-overlay-chart"]["figure"]["data"])


class TestLiveProbeArguments(LiveFixture):
    def namespace(self):
        return runpy.run_path(str(Path(__file__).resolve().parents[1] / "probes" / "dashboard_live_quote_overlay.py"))

    def test_demo_requires_no_database_and_production_root_is_excluded(self):
        namespace = self.namespace()
        with patch.object(sys, "argv", ["probe", "--demo"]):
            args = namespace["parse_args"]()
        self.assertTrue(args.demo)
        with patch.object(sys, "argv", ["probe", "output/quote_observation_journal_v2_api_opening_2026-10-06/2026-10-06.sqlite3"]):
            with self.assertRaises(SystemExit):
                namespace["parse_args"]()

    def test_simulator_uses_fresh_journal_and_synthetic_provenance(self):
        from threading import Event
        namespace = self.namespace()
        demo = namespace["OfflineDemo"](self.path.parent / "demo", "MSFT", Event())
        self.assertNotEqual(demo.path, self.path)
        events = tuple(__import__("mb_market_data.quote_journal_replay", fromlist=["QuoteJournalReplayReader"]).QuoteJournalReplayReader(demo.path).events())
        self.assertEqual(len(events), 2)
        self.assertTrue(events[0].revision.metadata["simulation"])
