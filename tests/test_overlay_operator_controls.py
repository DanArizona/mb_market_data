"""No-provider UI qualification for explicit, preview-first membership controls."""
from __future__ import annotations

import unittest
from typing import Any

from mb_market_data.quote_dashboard import create_quote_dashboard
from test_quote_dashboard import overlay_controller


class FakeAuthoritativeOperator:
    def __init__(self) -> None:
        self.preview_calls: list[tuple[str, str, str]] = []
        self.confirm_calls: list[str] = []
        self.receipt_calls: list[str] = []

    def preview(self, symbol: str, action: str, target: str) -> dict:
        self.preview_calls.append((symbol, action, target))
        if symbol == "BAD":
            raise ValueError("Worker is not currently eligible")
        return {
            "proposal_id": "proposal-one", "symbol": symbol,
            "action": action, "target": target,
            "before": "focus", "after": target,
            "based_on_revision": 47,
            "expires_at_utc": "2026-11-24T20:15:30Z",
            "note": "Preview only: no worker request yet",
            "request_written": False,
        }

    def confirm(self, proposal_id: str) -> dict:
        self.confirm_calls.append(proposal_id)
        return {
            "request": {"id": "manual-aaa111", "source": "manual"},
            "already_submitted": False,
        }

    def result(self, command_id: str) -> dict:
        self.receipt_calls.append(command_id)
        return {"id": command_id, "status": "PUBLISHED_PENDING_EFFECTIVE"}


def _component(node: Any, target: str) -> dict | None:
    if isinstance(node, dict):
        if node.get("props", {}).get("id") == target:
            return node
        for child in node.values():
            found = _component(child, target)
            if found is not None:
                return found
    if isinstance(node, list):
        for child in node:
            found = _component(child, target)
            if found is not None:
                return found
    return None


class TestOverlayOperatorControls(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = overlay_controller()
        self.controller.finish()
        self.backend = FakeAuthoritativeOperator()

    def app(self):
        return create_quote_dashboard(
            self.controller, live_mode=True, live_refresh=lambda: (0, "committed only"),
            operator_actions=self.backend,
        )

    def callback(self, app, output_id: str):
        return next((key, info) for key, info in app.callback_map.items()
                    if isinstance(info["output"], list)
                    and any(item.component_id == output_id for item in info["output"]))

    def invoke_operator(self, app, token: str, trigger: str, *,
                        symbol: str = "SPY", action: str = "promote",
                        target: str = "hot", proposal: dict | None = None):
        key, cb = self.callback(app, "operator-proposal")
        vals = {
            ("operator-preview-button", "n_clicks"): 1,
            ("operator-confirm-button", "n_clicks"): 1,
            ("operator-symbol", "value"): symbol,
            ("operator-action", "value"): action,
            ("operator-target", "value"): target,
            ("operator-proposal", "data"): proposal,
            ("operator-csrf", "data"): token,
        }
        client = app.server.test_client()
        response = client.post("/_dash-update-component", json={
            "output": key,
            "outputs": [
                {"id": item.component_id, "property": item.component_property}
                for item in cb["output"]
            ],
            "changedPropIds": [trigger],
            "inputs": [
                {"id": item["id"], "property": item["property"],
                 "value": vals[(item["id"], item["property"])]}
                for item in cb["inputs"]
            ],
            "state": [
                {"id": item["id"], "property": item["property"],
                 "value": vals[(item["id"], item["property"])]}
                for item in cb["state"]
            ],
        })
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        return response.get_json()["response"]

    def test_synthetic_banner_is_explicit_and_requires_operator_gate(self):
        with self.assertRaisesRegex(ValueError, "Synthetic operator labeling"):
            create_quote_dashboard(
                self.controller, live_mode=True,
                live_refresh=lambda: (0, "synthetic"),
                synthetic_operator_demo=True,
            )
        app = create_quote_dashboard(
            self.controller, live_mode=True,
            live_refresh=lambda: (0, "synthetic"),
            operator_actions=self.backend,
            synthetic_operator_demo=True,
        )
        response = app.server.test_client().get("/_dash-layout")
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        import json
        markup = json.dumps(response.get_json(), ensure_ascii=False)
        self.assertIn("OFFLINE SYNTHETIC MEMBERSHIP DEMO", markup)
        self.assertIn("NO SCHWAB / NO PRODUCTION JOURNAL", markup)
        self.assertIn("SYNTHETIC DEMO · controlled Membership", markup)
        self.assertIsNotNone(_component(response.get_json(), "operator-controls-panel"))

    def test_authoritative_operator_backend_imports_without_running_it(self):
        # Import-only smoke check: never construct a provider or submit a command.
        from mb_market_data.daily_operator_ui import OperatorState
        from mb_market_data.overlay_manual_membership import RealTimeMembershipOperator
        self.assertTrue(callable(OperatorState.preview))
        self.assertTrue(callable(OperatorState.confirm))
        self.assertTrue(callable(RealTimeMembershipOperator.preview))
        self.assertTrue(callable(RealTimeMembershipOperator.confirm))

    def test_default_modes_contain_no_operator_request_surface(self):
        for live in (False, True):
            with self.subTest(live=live):
                app = create_quote_dashboard(
                    self.controller, live_mode=live,
                    live_refresh=(lambda: (0, "ok")) if live else None,
                )
                response = app.server.test_client().get("/_dash-layout")
                self.addCleanup(response.close)
                self.assertNotIn(b'"id":"operator-confirm-button"', response.data)
                self.assertNotIn(b'"id":"operator-csrf"', response.data)
                self.assertEqual(self.backend.confirm_calls, [])
        with self.assertRaisesRegex(ValueError, "Historical"):
            create_quote_dashboard(self.controller, operator_actions=self.backend)

    def test_preview_is_nonmutating_and_confirm_requires_separate_click(self):
        app = self.app()
        response = app.server.test_client().get("/_dash-layout")
        self.addCleanup(response.close)
        token = _component(response.get_json(), "operator-csrf")["props"]["data"]
        self.assertTrue(token)
        controls = _component(response.get_json(), "operator-controls-panel")
        self.assertIsNotNone(controls)
        self.assertTrue(_component(controls, "operator-confirm-button")["props"]["disabled"])
        import json
        self.assertIn("REAL TIME · CONTROLLED", json.dumps(response.get_json(), ensure_ascii=False))

        preview = self.invoke_operator(app, token, "operator-preview-button.n_clicks")
        self.assertEqual(self.backend.preview_calls, [("SPY", "promote", "hot")])
        self.assertEqual(self.backend.confirm_calls, [])
        self.assertFalse(preview["operator-confirm-button"]["disabled"])
        self.assertEqual(preview["operator-proposal"]["data"]["proposal_id"], "proposal-one")
        self.assertIsNone(preview["operator-command"]["data"])
        self.assertIn("PREVIEW ONLY", preview["operator-preview-status"]["children"])

        submit = self.invoke_operator(
            app, token, "operator-confirm-button.n_clicks",
            proposal=preview["operator-proposal"]["data"],
        )
        self.assertEqual(self.backend.confirm_calls, ["proposal-one"])
        self.assertTrue(submit["operator-confirm-button"]["disabled"])
        self.assertEqual(submit["operator-command"]["data"], "manual-aaa111")
        self.assertIn("REQUEST QUEUED", submit["operator-submit-status"]["children"])
        self.assertNotIn("EFFECTIVE_AS_PUBLISHED", submit["operator-submit-status"]["children"])

    def test_missing_token_changed_fields_or_old_preview_never_submit(self):
        app = self.app()
        response = app.server.test_client().get("/_dash-layout")
        self.addCleanup(response.close)
        token = _component(response.get_json(), "operator-csrf")["props"]["data"]
        invalid = self.invoke_operator(
            app, "unauthorized", "operator-preview-button.n_clicks"
        )
        self.assertIn("authorization invalid", invalid["operator-preview-status"]["children"])
        self.assertEqual(self.backend.preview_calls, [])

        preview = self.invoke_operator(app, token, "operator-preview-button.n_clicks")
        stale = self.invoke_operator(
            app, token, "operator-confirm-button.n_clicks",
            target="uni", proposal=preview["operator-proposal"]["data"],
        )
        self.assertIn("changed since preview", stale["operator-preview-status"]["children"])
        self.assertIsNone(stale["operator-command"]["data"])
        self.assertEqual(self.backend.confirm_calls, [])
        changed = self.invoke_operator(app, token, "operator-symbol.value", symbol="QQQ",
                                       proposal=preview["operator-proposal"]["data"])
        self.assertIsNone(changed["operator-proposal"]["data"])
        self.assertTrue(changed["operator-confirm-button"]["disabled"])
        denied = self.invoke_operator(
            app, token, "operator-preview-button.n_clicks", symbol="BAD"
        )
        self.assertIn("Preview refused", denied["operator-preview-status"]["children"])
        self.assertEqual(self.backend.confirm_calls, [])

    def test_worker_receipt_status_is_read_only(self):
        app = self.app()
        callback_key, callback = next(
            (key, info) for key, info in app.callback_map.items()
            if getattr(info["output"], "component_id", None) == "operator-receipt-status"
        )
        response = app.server.test_client().post("/_dash-update-component", json={
            "output": callback_key,
            "outputs": {"id": "operator-receipt-status", "property": "children"},
            "changedPropIds": ["operator-command.data"],
            "inputs": [
                {"id": "operator-command", "property": "data", "value": "manual-aaa111"},
                {"id": "operator-receipt-tick", "property": "n_intervals", "value": 0},
            ],
            "state": [],
        })
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        self.assertIn("PUBLISHED_PENDING_EFFECTIVE",
                      response.get_json()["response"]["operator-receipt-status"]["children"])
        self.assertEqual(self.backend.receipt_calls, ["manual-aaa111"])
        self.assertEqual(self.backend.confirm_calls, [])


if __name__ == "__main__":
    unittest.main()
