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
        self.drop_calls: list[tuple[str, str, str]] = []
        self.receipt_status = "PUBLISHED_PENDING_EFFECTIVE"
        self.receipt_symbol = "SPY"
        self.receipt_target = "focus"

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

    def preview_drop(self, symbol: str, source_tier: str, destination_tier: str) -> dict:
        self.drop_calls.append((symbol, source_tier, destination_tier))
        ranks = ("uni", "focus", "hot")
        if source_tier not in ranks or destination_tier not in ranks or source_tier == destination_tier:
            raise ValueError("Invalid drop")
        # The production adapter separately verifies actual effective source.
        action = "promote" if ranks.index(destination_tier) > ranks.index(source_tier) else "demote"
        preview = self.preview(symbol, action, destination_tier)
        preview["before"] = source_tier
        return preview

    def confirm(self, proposal_id: str) -> dict:
        self.confirm_calls.append(proposal_id)
        return {
            "request": {"id": "manual-aaa111", "source": "manual"},
            "already_submitted": False,
        }

    def result(self, command_id: str) -> dict:
        self.receipt_calls.append(command_id)
        return {
            "id": command_id, "status": self.receipt_status,
            "symbol": self.receipt_symbol, "target": self.receipt_target,
        }


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
            ("membership-drag-request", "data"): None,
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
                self.assertNotIn(b'"id":"membership-drag-request"', response.data)
                for tier in ("uni", "focus", "hot"):
                    grid = _component(response.get_json(), f"membership-{tier}-grid")
                    self.assertFalse(grid["props"]["columnDefs"][0]["rowDrag"])
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

    def test_drag_only_previews_and_never_queues_without_confirm(self):
        app = self.app()
        layout = app.server.test_client().get("/_dash-layout")
        self.addCleanup(layout.close)
        self.assertEqual(layout.status_code, 200)
        self.assertIsNotNone(_component(layout.get_json(), "membership-drag-request"))
        self.assertIsNotNone(_component(layout.get_json(), "membership-drag-status"))
        self.assertIn("membership-drag-binding-status.data", app.callback_map)
        for tier in ("uni", "focus", "hot"):
            grid = _component(layout.get_json(), f"membership-{tier}-grid")
            self.assertTrue(grid["props"]["columnDefs"][0]["rowDrag"])
            self.assertTrue(grid["props"]["dashGridOptions"]["suppressRowDrag"])
            self.assertTrue(grid["props"]["dashGridOptions"]["suppressMoveWhenRowDragging"])

        token = _component(layout.get_json(), "operator-csrf")["props"]["data"]
        key, cb = self.callback(app, "operator-proposal")
        values = {
            "operator-preview-button": 0,
            "operator-confirm-button": 0,
            "operator-symbol": "INTC",
            "operator-action": "demote",
            "operator-target": "focus",
            "membership-drag-request": {
                "symbol": "INTC", "source_tier": "hot",
                "destination_tier": "focus", "nonce": "unique-1",
            },
        }
        response = app.server.test_client().post("/_dash-update-component", json={
            "output": key,
            "outputs": [
                {"id": item.component_id, "property": item.component_property}
                for item in cb["output"]
            ],
            "changedPropIds": ["membership-drag-request.data"],
            "inputs": [
                {"id": item["id"], "property": item["property"],
                 "value": values[item["id"]]}
                for item in cb["inputs"]
            ],
            "state": [
                {"id": "operator-proposal", "property": "data", "value": None},
                {"id": "operator-csrf", "property": "data", "value": token},
            ],
        })
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()["response"]
        self.assertEqual(self.backend.drop_calls, [("INTC", "hot", "focus")])
        self.assertEqual(self.backend.confirm_calls, [])
        self.assertEqual(body["operator-proposal"]["data"]["before"], "hot")
        self.assertEqual(body["operator-proposal"]["data"]["after"], "focus")
        self.assertIn("DRAG PREVIEW ONLY", body["operator-preview-status"]["children"])
        self.assertFalse(body["operator-confirm-button"]["disabled"])
        self.assertIsNone(body["operator-command"]["data"])

    def test_drop_backend_rechecks_actual_source_tier_before_preview(self):
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from datetime import datetime, timezone
        from mb_market_data.overlay_manual_membership import preview_validated_drop

        operator = SimpleNamespace(
            clock=lambda: datetime(2026, 11, 24, tzinfo=timezone.utc),
            preview=Mock(return_value={"proposal_id": "p-1"}),
        )
        with patch(
            "mb_market_data.overlay_manual_membership.membership_snapshot",
            return_value={"selected": {"tier": "hot"}},
        ) as roster:
            with self.assertRaisesRegex(ValueError, "Source tier changed"):
                preview_validated_drop(operator, Path("/offline"), "INTC", "focus", "uni")
            with self.assertRaisesRegex(ValueError, "different tiers"):
                preview_validated_drop(operator, Path("/offline"), "INTC", "hot", "hot")
            answer = preview_validated_drop(operator, Path("/offline"), "intc", "hot", "focus")
        self.assertEqual(answer["proposal_id"], "p-1")
        operator.preview.assert_called_once_with("INTC", "demote", "focus")
        self.assertEqual(roster.call_count, 2)

    def test_effective_worker_receipt_follows_matching_tier_only_once(self):
        app = self.app()
        self.backend.receipt_status = "PENDING"
        self.backend.receipt_symbol = "SPY"
        self.backend.receipt_target = "focus"
        key, cb = self.callback(app, "operator-follow-request")
        def check(seen=None):
            response = app.server.test_client().post("/_dash-update-component", json={
                "output": key,
                "outputs": [
                    {"id": item.component_id, "property": item.component_property}
                    for item in cb["output"]
                ],
                "changedPropIds": ["operator-receipt-tick.n_intervals"],
                "inputs": [
                    {
                        "id": item["id"], "property": item["property"],
                        "value": (
                            "manual-receipt-1" if item["id"] == "operator-command"
                            else 3 if item["id"] == "operator-receipt-tick"
                            else self.controller.snapshot().applied_event_count
                        ),
                    }
                    for item in cb["inputs"]
                ],
                "state": [
                    {"id": "operator-follow-seen", "property": "data", "value": seen},
                ],
            })
            self.addCleanup(response.close)
            return response

        pending = check()
        self.assertEqual(pending.status_code, 204)
        self.backend.receipt_status = "EFFECTIVE_AS_PUBLISHED"
        self.backend.receipt_target = "hot"  # current SPY is Focus, not Hot
        mismatched = check()
        self.assertEqual(mismatched.status_code, 204)
        self.backend.receipt_target = "focus"
        effective = check()
        self.assertEqual(effective.status_code, 200)
        values = effective.get_json()["response"]
        follow = values["operator-follow-request"]["data"]
        self.assertEqual(
            follow, {
                "command_id": "manual-receipt-1",
                "symbol": "SPY", "tier": "focus",
            },
        )
        self.assertEqual(
            values["operator-follow-seen"]["data"], "manual-receipt-1",
        )
        again = check(seen="manual-receipt-1")
        self.assertEqual(again.status_code, 204)

        select_key, select_cb = self.callback(app, "membership-selected-symbol")
        response = app.server.test_client().post("/_dash-update-component", json={
            "output": select_key,
            "outputs": [
                {"id": item.component_id, "property": item.component_property}
                for item in select_cb["output"]
            ],
            "changedPropIds": ["operator-follow-request.data"],
            "inputs": [
                {"id": item["id"], "property": item["property"],
                 "value": follow if item["id"] == "operator-follow-request" else None}
                for item in select_cb["inputs"]
            ],
            "state": [
                {"id": item["id"], "property": item["property"],
                 "value": "SPY" if item["id"] == "membership-search-symbol" else None}
                for item in select_cb["state"]
            ],
        })
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        updated = response.get_json()["response"]
        self.assertEqual(updated["membership-selected-symbol"]["data"], "SPY")
        self.assertEqual(
            updated["membership-focus-grid"]["scrollTo"],
            {"rowId": "SPY", "rowPosition": "middle"},
        )
        self.assertIn(
            "selected automatically",
            updated["membership-search-status"]["children"],
        )

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
