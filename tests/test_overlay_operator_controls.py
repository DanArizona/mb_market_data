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
                        target: str = "hot", proposal: dict | None = None,
                        drag_intent: dict | None = None):
        key, cb = self.callback(app, "operator-proposal")
        vals = {
            ("operator-confirm-button", "n_clicks"): 1,
            ("operator-symbol", "value"): symbol,
            ("operator-action", "value"): action,
            ("operator-target", "value"): target,
            ("operator-drag-intent", "data"): drag_intent,
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

    def test_unselected_manual_action_never_arms_confirm(self):
        app = self.app()
        layout = app.server.test_client().get("/_dash-layout")
        self.addCleanup(layout.close)
        controls = _component(layout.get_json(), "operator-controls-panel")
        self.assertIsNone(_component(controls, "operator-action")["props"]["value"])
        self.assertIsNone(_component(controls, "operator-target")["props"]["value"])
        self.assertTrue(
            _component(controls, "operator-confirm-button")["props"]["disabled"]
        )
        token = _component(layout.get_json(), "operator-csrf")["props"]["data"]
        incomplete = self.invoke_operator(
            app, token, "operator-action.value", action="demote", target=None,
        )
        self.assertTrue(incomplete["operator-confirm-button"]["disabled"])
        self.assertIsNone(incomplete["operator-proposal"]["data"])
        self.assertIn(
            "Choose an action and destination",
            incomplete["operator-preview-status"]["children"],
        )
        self.assertEqual(self.backend.preview_calls, [])
        self.assertEqual(self.backend.confirm_calls, [])
        complete = self.invoke_operator(
            app, token, "operator-target.value", action="demote", target="uni",
        )
        self.assertFalse(complete["operator-confirm-button"]["disabled"])
        self.assertEqual(complete["operator-proposal"]["data"]["target"], "uni")
        self.assertEqual(self.backend.confirm_calls, [])

    def test_manual_fields_auto_preview_and_single_confirm_queues_once(self):
        app = self.app()
        response = app.server.test_client().get("/_dash-layout")
        self.addCleanup(response.close)
        token = _component(response.get_json(), "operator-csrf")["props"]["data"]
        self.assertTrue(token)
        controls = _component(response.get_json(), "operator-controls-panel")
        self.assertIsNotNone(controls)
        self.assertTrue(_component(controls, "operator-confirm-button")["props"]["disabled"])
        self.assertIsNone(_component(controls, "operator-preview-button"))
        import json
        self.assertIn("REAL TIME · CONTROLLED", json.dumps(response.get_json(), ensure_ascii=False))

        preview = self.invoke_operator(app, token, "operator-action.value")
        self.assertEqual(self.backend.preview_calls, [("SPY", "promote", "hot")])
        self.assertEqual(self.backend.confirm_calls, [])
        self.assertFalse(preview["operator-confirm-button"]["disabled"])
        self.assertEqual(preview["operator-proposal"]["data"]["proposal_id"], "proposal-one")
        self.assertNotIn("operator-command", preview)
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

    def test_auto_follow_preserves_last_worker_receipt_after_symbol_changes(self):
        """A successful r3 move must not turn into 'No worker receipt.'."""
        app = self.app()
        layout = app.server.test_client().get("/_dash-layout")
        self.addCleanup(layout.close)
        token = _component(layout.get_json(), "operator-csrf")["props"]["data"]

        preview = self.invoke_operator(app, token, "operator-action.value")
        submitted = self.invoke_operator(
            app, token, "operator-confirm-button.n_clicks",
            proposal=preview["operator-proposal"]["data"],
        )
        self.assertEqual(submitted["operator-command"]["data"], "manual-aaa111")
        self.backend.receipt_status = "EFFECTIVE_AS_PUBLISHED"

        # Auto-selection after the effective revision echoes the symbol back
        # into operator controls. Only the *next preview* may be invalidated.
        selected = self.invoke_operator(
            app, token, "operator-symbol.value", symbol="SPY",
            proposal=None,
        )
        self.assertIn("AUTOMATIC PREVIEW ONLY", selected["operator-preview-status"]["children"])
        self.assertNotIn("operator-command", selected)
        self.assertNotIn("operator-submit-status", selected)
        self.assertFalse(selected["operator-confirm-button"]["disabled"])

        # New previews and denied previews must leave the last receipt intact.
        another = self.invoke_operator(
            app, token, "operator-action.value", symbol="SPY",
        )
        self.assertNotIn("operator-command", another)
        self.assertNotIn("operator-submit-status", another)
        failed = self.invoke_operator(
            app, token, "operator-action.value", symbol="BAD",
        )
        self.assertNotIn("operator-command", failed)
        self.assertNotIn("operator-submit-status", failed)

        command_key, command_cb = next(
            (key, info) for key, info in app.callback_map.items()
            if getattr(info["output"], "component_id", None)
            == "operator-receipt-status"
        )
        response = app.server.test_client().post("/_dash-update-component", json={
            "output": command_key,
            "outputs": {"id": "operator-receipt-status", "property": "children"},
            "changedPropIds": ["operator-receipt-tick.n_intervals"],
            "inputs": [
                {"id": "operator-command", "property": "data",
                 "value": submitted["operator-command"]["data"]},
                {"id": "operator-receipt-tick", "property": "n_intervals",
                 "value": 5},
            ],
        })
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        visible = response.get_json()["response"]["operator-receipt-status"]["children"]
        self.assertIn("manual-aaa111", visible)
        self.assertIn("EFFECTIVE_AS_PUBLISHED", visible)
        self.assertEqual(self.backend.confirm_calls, ["proposal-one"])

    def test_missing_token_changed_fields_or_old_preview_never_submit(self):
        app = self.app()
        response = app.server.test_client().get("/_dash-layout")
        self.addCleanup(response.close)
        token = _component(response.get_json(), "operator-csrf")["props"]["data"]
        invalid = self.invoke_operator(
            app, "unauthorized", "operator-action.value"
        )
        self.assertIn("authorization invalid", invalid["operator-preview-status"]["children"])
        self.assertEqual(self.backend.preview_calls, [])

        preview = self.invoke_operator(app, token, "operator-action.value")
        stale = self.invoke_operator(
            app, token, "operator-confirm-button.n_clicks",
            target="uni", proposal=preview["operator-proposal"]["data"],
        )
        self.assertIn("changed since preview", stale["operator-preview-status"]["children"])
        self.assertNotIn("operator-command", stale)
        self.assertEqual(self.backend.confirm_calls, [])
        changed = self.invoke_operator(app, token, "operator-symbol.value", symbol="QQQ",
                                       proposal=preview["operator-proposal"]["data"])
        self.assertEqual(changed["operator-proposal"]["data"]["symbol"], "QQQ")
        self.assertIn("AUTOMATIC PREVIEW ONLY", changed["operator-preview-status"]["children"])
        self.assertFalse(changed["operator-confirm-button"]["disabled"])
        denied = self.invoke_operator(
            app, token, "operator-action.value", symbol="BAD"
        )
        self.assertIn("Automatic preview unavailable", denied["operator-preview-status"]["children"])
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
            "operator-confirm-button": 0,
            "operator-symbol": "INTC",
            "operator-action": "demote",
            "operator-target": "focus",
            "operator-drag-intent": {
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
            "changedPropIds": ["operator-drag-intent.data"],
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
        self.assertNotIn("operator-command", body)

    def test_operator_preview_refuses_noop_same_tier_demotions(self):
        from pathlib import Path
        from unittest.mock import patch
        from mb_market_data.daily_operator_ui import OperatorState, OperatorProblem

        view = {
            "eligible": True,
            "pending_revision": False,
            "effective_revision": 3,
            "published_revision": 3,
            "selected": {"tier": "focus"},
            "session_date": "2026-11-24",
            "counts": {"uni": 25, "focus": 7, "hot": 3},
        }
        state = OperatorState(Path("/synthetic-demo-not-read"))
        with patch(
            "mb_market_data.daily_operator_ui.membership_snapshot",
            return_value=view,
        ):
            with self.assertRaisesRegex(OperatorProblem, "strictly lower tier"):
                state.preview("INTC", "demote", "focus")
            with self.assertRaisesRegex(OperatorProblem, "Choose promote"):
                state.preview("INTC", "demote", "hot")
            with self.assertRaisesRegex(OperatorProblem, "already at or above"):
                state.preview("INTC", "promote", "focus")
            # A genuine Focus → Uni demotion can still be previewed.
            allowed = state.preview("INTC", "demote", "uni")
        self.assertEqual(allowed["before"], "focus")
        self.assertEqual(allowed["after"], "uni")
        self.assertEqual(allowed["request_written"], False)

    def test_drag_miss_is_visible_and_never_queues_a_request(self):
        from pathlib import Path
        app = self.app()
        response = app.server.test_client().get("/_dash-layout")
        self.addCleanup(response.close)
        self.assertIsNotNone(
            _component(response.get_json(), "membership-drag-diagnostic")
        )
        drag_js = (
            Path(__file__).resolve().parents[1]
            / "src" / "mb_market_data" / "assets"
            / "membership_drag_bridge.js"
        ).read_text(encoding="utf-8")
        self.assertIn('api.addEventListener("dragStopped", onStopped)', drag_js)
        self.assertIn("document.elementFromPoint(", drag_js)
        self.assertIn('notify("drop-missed"', drag_js)
        self.assertIn('notify("drop-received"', drag_js)
        self.assertIn("registry.lastIssued", drag_js)

        key, cb = next(
            (key, value) for key, value in app.callback_map.items()
            if getattr(value["output"], "component_id", None)
            == "membership-drag-status"
        )
        reply = app.server.test_client().post("/_dash-update-component", json={
            "output": key,
            "outputs": {
                "id": "membership-drag-status", "property": "children",
            },
            "changedPropIds": ["membership-drag-diagnostic.data"],
            "inputs": [
                {"id": "membership-drag-binding-status", "property": "data",
                 "value": {"ready": True}},
                {"id": "membership-drag-diagnostic", "property": "data",
                 "value": {
                     "phase": "drop-missed",
                     "reason": "Drop not registered. Release over another tier list, then retry.",
                     "nonce": "drag-3",
                 }},
            ],
        })
        self.addCleanup(reply.close)
        self.assertEqual(reply.status_code, 200)
        self.assertIn(
            "Drop not registered",
            reply.get_json()["response"]["membership-drag-status"]["children"],
        )
        self.assertEqual(self.backend.preview_calls, [])
        self.assertEqual(self.backend.confirm_calls, [])

    def test_drag_reflection_is_atomic_and_uses_only_confirm_button(self):
        app = self.app()
        layout = app.server.test_client().get("/_dash-layout")
        self.addCleanup(layout.close)
        controls = _component(layout.get_json(), "operator-controls-panel")
        self.assertIsNone(_component(controls, "operator-preview-button"))
        self.assertIsNotNone(_component(controls, "operator-confirm-button"))
        self.assertIsNone(
            _component(controls, "operator-action")["props"]["value"]
        )
        self.assertIsNone(
            _component(controls, "operator-target")["props"]["value"]
        )

        callback_key, callback = self.callback(app, "operator-symbol")
        drag = {
            "symbol": "INTC",
            "source_tier": "hot",
            "destination_tier": "focus",
            "nonce": "drag-01",
        }
        response = app.server.test_client().post("/_dash-update-component", json={
            "output": callback_key,
            "outputs": [
                {"id": item.component_id, "property": item.component_property}
                for item in callback["output"]
            ],
            "changedPropIds": ["membership-drag-request.data"],
            "inputs": [
                {"id": "membership-selected-symbol", "property": "data",
                 "value": "INTC"},
                {"id": "membership-drag-request", "property": "data",
                 "value": drag},
            ],
        })
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        reflected = response.get_json()["response"]
        self.assertEqual(reflected["operator-symbol"]["value"], "INTC")
        self.assertEqual(reflected["operator-action"]["value"], "demote")
        self.assertEqual(reflected["operator-target"]["value"], "focus")
        self.assertEqual(reflected["operator-drag-intent"]["data"], drag)
        self.assertEqual(self.backend.preview_calls, [])
        self.assertEqual(self.backend.confirm_calls, [])

    def test_auto_follow_does_not_arm_reverse_action(self):
        """Worker-driven selection cannot silently change demote to promote."""
        app = self.app()
        key, cb = self.callback(app, "operator-symbol")
        response = app.server.test_client().post("/_dash-update-component", json={
            "output": key,
            "outputs": [
                {"id": item.component_id, "property": item.component_property}
                for item in cb["output"]
            ],
            "changedPropIds": ["membership-selected-symbol.data"],
            "inputs": [
                {"id": "membership-selected-symbol", "property": "data",
                 "value": "INTC"},
                {"id": "membership-drag-request", "property": "data",
                 "value": None},
            ],
        })
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        changed = response.get_json()["response"]
        self.assertEqual(changed["operator-symbol"]["value"], "INTC")
        self.assertNotIn("operator-action", changed)
        self.assertNotIn("operator-target", changed)
        self.assertNotIn("operator-drag-intent", changed)
        self.assertEqual(self.backend.preview_calls, [])
        self.assertEqual(self.backend.confirm_calls, [])

    def test_stale_drag_control_values_cannot_enable_confirm(self):
        app = self.app()
        layout = app.server.test_client().get("/_dash-layout")
        self.addCleanup(layout.close)
        token = _component(layout.get_json(), "operator-csrf")["props"]["data"]
        drag = {
            "symbol": "INTC", "source_tier": "hot",
            "destination_tier": "focus", "nonce": "drag-02",
        }
        # Browser has received the drag but the old "Promote" field has not
        # yet been replaced by the new "Ordinary demote" control value.
        stale = self.invoke_operator(
            app, token, "operator-drag-intent.data", symbol="INTC",
            action="promote", target="focus", drag_intent=drag,
        )
        self.assertIn(
            "synchronizing", stale["operator-preview-status"]["children"]
        )
        self.assertTrue(stale["operator-confirm-button"]["disabled"])
        self.assertEqual(self.backend.drop_calls, [])
        self.assertEqual(self.backend.confirm_calls, [])

        # The reflected action, target and source arrive together.
        ready = self.invoke_operator(
            app, token, "operator-drag-intent.data", symbol="INTC",
            action="demote", target="focus", drag_intent=drag,
        )
        self.assertEqual(
            self.backend.drop_calls, [("INTC", "hot", "focus")]
        )
        self.assertIn(
            "DRAG PREVIEW ONLY", ready["operator-preview-status"]["children"]
        )
        self.assertFalse(ready["operator-confirm-button"]["disabled"])
        self.assertNotIn("operator-command", ready)

        # Any later changed target must be re-previewed, not blindly queued.
        changed = self.invoke_operator(
            app, token, "operator-target.value", symbol="INTC",
            action="demote", target="uni",
            proposal=ready["operator-proposal"]["data"],
        )
        self.assertIn(
            "AUTOMATIC PREVIEW ONLY", changed["operator-preview-status"]["children"]
        )
        self.assertEqual(
            changed["operator-proposal"]["data"]["target"], "uni"
        )
        self.assertEqual(self.backend.confirm_calls, [])

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
                            else [{"symbol": "SPY"}] if item["id"] == "membership-focus-grid"
                            else [] if item["id"] in {
                                "membership-uni-grid", "membership-hot-grid"
                            } else self.controller.snapshot().applied_event_count
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
