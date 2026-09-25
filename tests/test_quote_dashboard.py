from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from threading import Event
from typing import Any

from mb_market_data.observation_overlay import (
    ET as OVERLAY_ET,
    ObservationOverlayOHLCVCache,
    ObservationOverlayProjector,
    OverlayCandle,
)
from mb_market_data.quote_dashboard import (
    _column_definitions,
    _parse_seek_time_utc,
    create_quote_dashboard,
)
from mb_market_data.quote_dashboard_replay import (
    QuoteDashboardReplayController,
)
from mb_market_data.quote_journal_replay import (
    ChannelRevisionEvent,
    MembershipRevisionEvent,
    ReplayTimeline,
)
from mb_market_data.quote_observation_store import SamplingChannelRevision
from mb_market_data.sampling_membership import SamplingHierarchyRevision


SESSION_DATE = date(2026, 9, 11)


def single_event_controller() -> QuoteDashboardReplayController:
    available_at = datetime(2026, 9, 11, 13, 30, tzinfo=timezone.utc)
    event = ChannelRevisionEvent(
        SamplingChannelRevision(
            channel="uni",
            session_date=SESSION_DATE,
            revision=0,
            effective_at=available_at,
            symbols=("SPY",),
            source="unit-test",
        )
    )
    return QuoteDashboardReplayController(
        timeline=ReplayTimeline(
            SESSION_DATE,
            1,
            available_at,
            available_at,
        ),
        event_factory=lambda: iter((event,)),
    )


def two_event_controller() -> QuoteDashboardReplayController:
    first = datetime(2026, 9, 11, 13, 30, tzinfo=timezone.utc)
    events = (
        ChannelRevisionEvent(
            SamplingChannelRevision(
                channel="uni",
                session_date=SESSION_DATE,
                revision=0,
                effective_at=first,
                symbols=("SPY",),
                source="unit-test",
            )
        ),
        ChannelRevisionEvent(
            SamplingChannelRevision(
                channel="focus",
                session_date=SESSION_DATE,
                revision=0,
                effective_at=first.replace(second=10),
                symbols=("AAPL",),
                source="unit-test",
            )
        ),
    )
    return QuoteDashboardReplayController(
        timeline=ReplayTimeline(
            SESSION_DATE,
            len(events),
            events[0].available_at_utc,
            events[-1].available_at_utc,
        ),
        event_factory=lambda: iter(events),
    )


def overlay_controller() -> QuoteDashboardReplayController:
    available_at = datetime(2026, 9, 11, 13, 30, tzinfo=timezone.utc)
    event = MembershipRevisionEvent(
        SamplingHierarchyRevision(
            session_date=SESSION_DATE,
            revision=0,
            effective_at=available_at,
            uni_symbols=("SPY",),
            focus_symbols=("SPY",),
            hot_symbols=(),
            source="unit-test",
        )
    )
    cache = ObservationOverlayOHLCVCache(
        symbol="SPY",
        session_date=SESSION_DATE,
        provider="Schwab",
        source="unit-test",
        acquired_at_utc=available_at + timedelta(hours=8),
        request_start_et=datetime(
            2026, 9, 11, 0, 0, tzinfo=OVERLAY_ET
        ),
        request_end_et=datetime(
            2026, 9, 11, 16, 5, tzinfo=OVERLAY_ET
        ),
        source_payload_sha256="a" * 64,
        candles=(
            OverlayCandle(
                symbol="SPY",
                start_et=datetime(
                    2026, 9, 11, 9, 25, tzinfo=OVERLAY_ET
                ),
                open=100,
                high=102,
                low=99,
                close=101,
                volume=10_000,
            ),
        ),
    )
    return QuoteDashboardReplayController(
        timeline=ReplayTimeline(
            SESSION_DATE,
            1,
            available_at,
            available_at,
        ),
        event_factory=lambda: iter((event,)),
        overlay_projector_factory=lambda: ObservationOverlayProjector(
            cache=cache,
            symbol=cache.symbol,
            session_date=SESSION_DATE,
        ),
    )


def replay_callback_payload(
    app: Any,
    *,
    changed_prop_id: str,
    rendered_event_count: int,
    seek_time: str | None = None,
    speed: float = 60,
) -> tuple[str, dict[str, Any]]:
    callback_key, callback = next(
        (key, value)
        for key, value in app.callback_map.items()
        if any(item["id"] == "replay-toggle" for item in value["inputs"])
    )
    outputs = [
        {
            "id": item.component_id,
            "property": item.component_property,
        }
        for item in callback["output"]
    ]
    inputs = [
        {
            "id": "replay-toggle",
            "property": "n_clicks",
            "value": 1
            if changed_prop_id == "replay-toggle.n_clicks"
            else None,
        },
        {
            "id": "replay-step",
            "property": "n_clicks",
            "value": 1
            if changed_prop_id == "replay-step.n_clicks"
            else None,
        },
        {
            "id": "replay-restart",
            "property": "n_clicks",
            "value": 1
            if changed_prop_id == "replay-restart.n_clicks"
            else None,
        },
        {
            "id": "replay-seek",
            "property": "n_clicks",
            "value": 1
            if changed_prop_id == "replay-seek.n_clicks"
            else None,
        },
        {
            "id": "replay-tick",
            "property": "n_intervals",
            "value": 0,
        },
        {
            "id": "replay-speed",
            "property": "value",
            "value": speed,
        },
    ]
    return callback_key, {
        "output": callback_key,
        "outputs": outputs,
        "changedPropIds": [changed_prop_id],
        "inputs": inputs,
        "state": [
            {
                "id": "rendered-event-count",
                "property": "data",
                "value": rendered_event_count,
            },
            {
                "id": "replay-seek-time",
                "property": "value",
                "value": seek_time,
            },
        ],
    }


class TestQuoteDashboard(unittest.TestCase):
    def test_optional_observation_overlay_is_in_layout_and_callbacks(self) -> None:
        controller = overlay_controller()
        controller.finish()
        app = create_quote_dashboard(controller)
        client = app.server.test_client()

        layout = client.get("/_dash-layout")
        self.addCleanup(layout.close)

        self.assertEqual(layout.status_code, 200)
        self.assertIn(b"observation-overlay-panel", layout.data)
        self.assertIn(b"observation-overlay-chart", layout.data)
        self.assertIn(b"Observation Overlay", layout.data)
        self.assertIn(b"Focus", layout.data)
        overlay_callbacks = [
            (key, callback)
            for key, callback in app.callback_map.items()
            if any(
                item["id"] == "replay-clock"
                for item in callback["inputs"]
            )
        ]
        self.assertEqual(len(overlay_callbacks), 1)
        callback_key, callback = overlay_callbacks[0]
        outputs = [
            {
                "id": item.component_id,
                "property": item.component_property,
            }
            for item in callback["output"]
        ]
        response = client.post(
            "/_dash-update-component",
            json={
                "output": callback_key,
                "outputs": outputs,
                "changedPropIds": ["replay-clock.children"],
                "inputs": [
                    {
                        "id": "replay-clock",
                        "property": "children",
                        "value": "2026-09-11 09:30:00.000 ET",
                    },
                    {
                        "id": "theme-preference",
                        "property": "modified_timestamp",
                        "value": 0,
                    },
                ],
                "state": [
                    {
                        "id": "theme-preference",
                        "property": "data",
                        "value": "dark",
                    }
                ],
            },
        )
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()["response"]
        self.assertEqual(
            body["observation-overlay-band"]["children"],
            "Focus",
        )
        traces = body["observation-overlay-chart"]["figure"]["data"]
        self.assertEqual(
            tuple(trace["name"] for trace in traces),
            ("5-minute OHLC", "Volume"),
        )

    def test_explains_membership_and_quote_provenance_columns(self) -> None:
        columns = {
            column["field"]: column for column in _column_definitions()
        }

        for field in (
            "uni_revision",
            "focus_revision",
            "hot_revision",
            "latest_channel",
            "mark",
        ):
            self.assertTrue(columns[field]["headerTooltip"])

    def test_serves_layout_and_packaged_assets(self) -> None:
        controller = QuoteDashboardReplayController(
            timeline=ReplayTimeline(date(2026, 9, 11), 0, None, None),
            event_factory=lambda: iter(()),
        )
        stop_event = Event()
        app = create_quote_dashboard(
            controller,
            request_server_stop=stop_event.set,
        )
        client = app.server.test_client()

        page = client.get("/")
        layout = client.get("/_dash-layout")
        stylesheet = client.get("/assets/quote_dashboard.css")
        favicon = client.get("/assets/favicon.svg")
        for response in (page, layout, stylesheet, favicon):
            self.addCleanup(response.close)

        self.assertEqual(page.status_code, 200)
        self.assertEqual(layout.status_code, 200)
        self.assertIn(b"market-state-grid", layout.data)
        self.assertIn(b"replay-toggle", layout.data)
        self.assertIn(b"replay-seek-time", layout.data)
        self.assertIn(b"replay-seek", layout.data)
        self.assertIn(b"theme-toggle", layout.data)
        self.assertIn(b"theme-preference", layout.data)
        self.assertIn(b"replay-speed-dropdown", layout.data)
        self.assertIn(b"server-stop", layout.data)
        self.assertNotIn(b"observation-overlay-panel", layout.data)
        self.assertEqual(len(app.callback_map), 5)
        self.assertEqual(stylesheet.status_code, 200)
        self.assertIn(b"@media print", stylesheet.data)
        self.assertIn(b".dash-dropdown-content", stylesheet.data)
        self.assertEqual(favicon.status_code, 200)

    def test_parses_seek_time_as_end_of_displayed_et_second(self) -> None:
        self.assertEqual(
            _parse_seek_time_utc(SESSION_DATE, " 09:30:00 "),
            datetime(
                2026,
                9,
                11,
                13,
                30,
                0,
                999_999,
                tzinfo=timezone.utc,
            ),
        )
        self.assertEqual(
            _parse_seek_time_utc(SESSION_DATE, "00:00:00"),
            datetime(
                2026,
                9,
                11,
                4,
                0,
                0,
                999_999,
                tzinfo=timezone.utc,
            ),
        )
        self.assertEqual(
            _parse_seek_time_utc(SESSION_DATE, "23:59:59"),
            datetime(
                2026,
                9,
                12,
                3,
                59,
                59,
                999_999,
                tzinfo=timezone.utc,
            ),
        )
        self.assertEqual(
            _parse_seek_time_utc(date(2026, 1, 15), "09:30:00"),
            datetime(
                2026,
                1,
                15,
                14,
                30,
                0,
                999_999,
                tzinfo=timezone.utc,
            ),
        )

    def test_rejects_invalid_seek_time_text(self) -> None:
        for value in (
            None,
            "",
            "9:30:00",
            "09:30",
            "09:30:00.5",
            "24:00:00",
            "09:60:00",
            "09:30:60",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _parse_seek_time_utc(SESSION_DATE, value)

    def test_confirmed_server_stop_requests_shutdown(self) -> None:
        stop_event = Event()
        app = create_quote_dashboard(
            single_event_controller(),
            request_server_stop=stop_event.set,
        )
        client = app.server.test_client()

        confirm_response = client.post(
            "/_dash-update-component",
            json={
                "output": "server-stop-confirm.displayed",
                "outputs": {
                    "id": "server-stop-confirm",
                    "property": "displayed",
                },
                "changedPropIds": ["server-stop.n_clicks"],
                "inputs": [
                    {
                        "id": "server-stop",
                        "property": "n_clicks",
                        "value": 1,
                    }
                ],
                "state": [],
            },
        )
        self.addCleanup(confirm_response.close)
        self.assertEqual(confirm_response.status_code, 200)
        confirm_body = confirm_response.get_json()["response"]
        self.assertTrue(confirm_body["server-stop-confirm"]["displayed"])

        stop_response = client.post(
            "/_dash-update-component",
            json={
                "output": "server-stop-status.children",
                "outputs": {
                    "id": "server-stop-status",
                    "property": "children",
                },
                "changedPropIds": [
                    "server-stop-confirm.submit_n_clicks"
                ],
                "inputs": [
                    {
                        "id": "server-stop-confirm",
                        "property": "submit_n_clicks",
                        "value": 1,
                    }
                ],
                "state": [],
            },
        )
        self.addCleanup(stop_response.close)
        self.assertEqual(stop_response.status_code, 200)
        stop_body = stop_response.get_json()["response"]
        self.assertEqual(
            stop_body["server-stop-status"]["children"],
            "Stopping…",
        )
        self.assertTrue(stop_event.wait(1.0))

    def test_theme_callbacks_toggle_and_apply_saved_preference(self) -> None:
        app = create_quote_dashboard(single_event_controller())
        client = app.server.test_client()
        remember_key, remember = next(
            (key, value)
            for key, value in app.callback_map.items()
            if any(
                item["id"] == "theme-toggle"
                for item in value["inputs"]
            )
        )
        remember_response = client.post(
            "/_dash-update-component",
            json={
                "output": remember_key,
                "outputs": {
                    "id": "theme-preference",
                    "property": "data",
                },
                "changedPropIds": ["theme-toggle.n_clicks"],
                "inputs": [
                    {
                        "id": "theme-toggle",
                        "property": "n_clicks",
                        "value": 1,
                    }
                ],
                "state": [
                    {
                        "id": "theme-preference",
                        "property": "data",
                        "value": "dark",
                    }
                ],
            },
        )
        self.addCleanup(remember_response.close)

        self.assertEqual(remember_response.status_code, 200)
        self.assertEqual(
            remember_response.get_json()["response"]["theme-preference"][
                "data"
            ],
            "light",
        )

        apply_key, apply_callback = next(
            (key, value)
            for key, value in app.callback_map.items()
            if any(
                item["id"] == "theme-preference"
                and item["property"] == "modified_timestamp"
                for item in value["inputs"]
            )
        )
        apply_outputs = [
            {
                "id": item.component_id,
                "property": item.component_property,
            }
            for item in apply_callback["output"]
        ]
        apply_response = client.post(
            "/_dash-update-component",
            json={
                "output": apply_key,
                "outputs": apply_outputs,
                "changedPropIds": [
                    "theme-preference.modified_timestamp"
                ],
                "inputs": [
                    {
                        "id": "theme-preference",
                        "property": "modified_timestamp",
                        "value": 1,
                    }
                ],
                "state": [
                    {
                        "id": "theme-preference",
                        "property": "data",
                        "value": "light",
                    }
                ],
            },
        )
        self.addCleanup(apply_response.close)

        self.assertEqual(apply_response.status_code, 200)
        body = apply_response.get_json()["response"]
        self.assertEqual(
            body["dashboard-page"]["className"],
            "dashboard-shell theme-light",
        )
        self.assertEqual(body["theme-toggle"]["children"], "Dark mode")

    def test_play_callback_projects_and_returns_updated_state(self) -> None:
        controller = single_event_controller()
        app = create_quote_dashboard(controller)
        controller.set_speed(390)
        callback_key, payload = replay_callback_payload(
            app,
            changed_prop_id="replay-toggle.n_clicks",
            rendered_event_count=0,
        )

        response = app.server.test_client().post(
            "/_dash-update-component",
            json=payload,
        )
        self.addCleanup(response.close)

        self.assertEqual(response.status_code, 200)
        body = response.get_json()["response"]
        self.assertEqual(
            body["replay-status"]["children"],
            "End of recorded data",
        )
        self.assertEqual(body["metric-events"]["children"], "1")
        self.assertEqual(controller.snapshot().speed, 60)
        self.assertEqual(
            body["market-state-grid"]["rowData"][0]["symbol"],
            "SPY",
        )

    def test_seek_callback_projects_through_requested_second(self) -> None:
        controller = two_event_controller()
        app = create_quote_dashboard(controller)
        callback_key, payload = replay_callback_payload(
            app,
            changed_prop_id="replay-seek.n_clicks",
            rendered_event_count=0,
            seek_time="09:30:05",
        )

        response = app.server.test_client().post(
            "/_dash-update-component",
            json=payload,
        )
        self.addCleanup(response.close)

        self.assertEqual(response.status_code, 200)
        body = response.get_json()["response"]
        self.assertEqual(body["replay-status"]["children"], "Paused")
        self.assertEqual(
            body["replay-clock"]["children"],
            "2026-09-11 09:30:05.999 ET",
        )
        self.assertEqual(body["metric-events"]["children"], "1")
        self.assertEqual(
            body["market-state-grid"]["rowData"][0]["symbol"],
            "SPY",
        )
        snapshot = controller.snapshot()
        self.assertEqual(snapshot.applied_event_count, 1)
        self.assertEqual(
            snapshot.state.current_time_utc,
            datetime(2026, 9, 11, 13, 30, tzinfo=timezone.utc),
        )

    def test_invalid_seek_callback_preserves_replay_state(self) -> None:
        controller = two_event_controller()
        app = create_quote_dashboard(controller)
        baseline = controller.snapshot()
        callback_key, payload = replay_callback_payload(
            app,
            changed_prop_id="replay-seek.n_clicks",
            rendered_event_count=0,
            seek_time="9:30",
        )

        response = app.server.test_client().post(
            "/_dash-update-component",
            json=payload,
        )
        self.addCleanup(response.close)

        self.assertEqual(response.status_code, 200)
        body = response.get_json()["response"]
        self.assertEqual(
            body["replay-status"]["children"],
            "Seek time must use HH:MM:SS ET.",
        )
        self.assertEqual(
            body["replay-status"]["className"],
            "replay-status status-error",
        )
        self.assertEqual(controller.snapshot(), baseline)


if __name__ == "__main__":
    unittest.main()
