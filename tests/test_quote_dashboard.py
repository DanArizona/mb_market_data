from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from mb_market_data.quote_dashboard import create_quote_dashboard
from mb_market_data.quote_dashboard_replay import (
    QuoteDashboardReplayController,
)
from mb_market_data.quote_journal_replay import (
    ChannelRevisionEvent,
    ReplayTimeline,
)
from mb_market_data.quote_observation_store import SamplingChannelRevision


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


class TestQuoteDashboard(unittest.TestCase):
    def test_serves_layout_and_packaged_assets(self) -> None:
        controller = QuoteDashboardReplayController(
            timeline=ReplayTimeline(date(2026, 9, 11), 0, None, None),
            event_factory=lambda: iter(()),
        )
        app = create_quote_dashboard(controller)
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
        self.assertIn(b"theme-toggle", layout.data)
        self.assertIn(b"theme-preference", layout.data)
        self.assertIn(b"replay-speed-dropdown", layout.data)
        self.assertEqual(len(app.callback_map), 3)
        self.assertEqual(stylesheet.status_code, 200)
        self.assertIn(b"@media print", stylesheet.data)
        self.assertIn(b".dash-dropdown-content", stylesheet.data)
        self.assertEqual(favicon.status_code, 200)

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
        app = create_quote_dashboard(single_event_controller())
        callback_key, callback = next(
            (key, value)
            for key, value in app.callback_map.items()
            if any(
                item["id"] == "replay-toggle"
                for item in value["inputs"]
            )
        )
        outputs = [
            {
                "id": item.component_id,
                "property": item.component_property,
            }
            for item in callback["output"]
        ]
        payload = {
            "output": callback_key,
            "outputs": outputs,
            "changedPropIds": ["replay-toggle.n_clicks"],
            "inputs": [
                {
                    "id": "replay-toggle",
                    "property": "n_clicks",
                    "value": 1,
                },
                {
                    "id": "replay-step",
                    "property": "n_clicks",
                    "value": None,
                },
                {
                    "id": "replay-restart",
                    "property": "n_clicks",
                    "value": None,
                },
                {
                    "id": "replay-tick",
                    "property": "n_intervals",
                    "value": 0,
                },
                {
                    "id": "replay-speed",
                    "property": "value",
                    "value": 60,
                },
            ],
            "state": [
                {
                    "id": "rendered-event-count",
                    "property": "data",
                    "value": 0,
                }
            ],
        }

        response = app.server.test_client().post(
            "/_dash-update-component",
            json=payload,
        )
        self.addCleanup(response.close)

        self.assertEqual(response.status_code, 200)
        body = response.get_json()["response"]
        self.assertEqual(body["replay-status"]["children"], "Complete")
        self.assertEqual(body["metric-events"]["children"], "1")
        self.assertEqual(
            body["market-state-grid"]["rowData"][0]["symbol"],
            "SPY",
        )


if __name__ == "__main__":
    unittest.main()
