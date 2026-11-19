"""Synthetic-only integration contract for the Overlay membership controls."""
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from mb_market_data.daily_operator_demo import DemoSession
from mb_market_data.daily_operator_ui import OperatorProblem
from mb_market_data.overlay_membership_bridge import SyntheticOverlayMembership
from mb_market_data.quote_dashboard import create_quote_dashboard
from test_quote_dashboard import overlay_controller


def descendants(component):
    yield component
    children = getattr(component, "children", None)
    if isinstance(children, (list, tuple)):
        for child in children:
            yield from descendants(child)
    elif children is not None and not isinstance(children, (str, int, float, bool)):
        yield from descendants(children)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="mb_bridge_test_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.demo = DemoSession(self.root)
        self.demo.start()
        self.addCleanup(self.demo.close)
        self.bridge = SyntheticOverlayMembership(self.root)
        for _ in range(60):
            if self.bridge.snapshot()["eligible"]:
                break
            time.sleep(.05)
        self.assertTrue(self.bridge.snapshot()["eligible"], "Synthetic worker did not become ready")

    def test_only_synthetic_plan_can_construct_bridge(self):
        from mb_market_data.acquisition_health import atomic_json
        from mb_market_data.daily_membership import read
        plan = read(self.root / "plan.json")
        plan["operator_demo_synthetic"] = False
        atomic_json(self.root / "plan.json", plan)
        with self.assertRaisesRegex(ValueError, "synthetic-only"):
            SyntheticOverlayMembership(self.root)

    def test_snapshot_preserves_exclusive_nested_membership_and_quote(self):
        view = self.bridge.snapshot("INTC")
        self.assertTrue(view["synthetic_demo"])
        self.assertTrue(view["eligible"])
        self.assertEqual(view["selected"]["tier"], "hot")
        self.assertIn("INTC", [row["symbol"] for row in self.bridge.records()["hot"]])
        self.assertEqual(view["counts"]["hot"], 4)

    def test_historical_mode_refuses_preview_and_confirm(self):
        with self.assertRaisesRegex(OperatorProblem, "read-only"):
            self.bridge.preview("historical", "INTC", "demote", "focus")
        proposal = self.bridge.preview("real-time", "INTC", "demote", "focus")
        self.assertFalse(proposal["request_written"])
        with self.assertRaisesRegex(OperatorProblem, "read-only"):
            self.bridge.confirm("historical", proposal["proposal_id"])

    def test_preview_confirm_uses_existing_worker_inbox(self):
        proposal = self.bridge.preview("real-time", "INTC", "demote", "focus")
        request = self.bridge.confirm("real-time", proposal["proposal_id"])
        self.assertEqual(request["request"]["symbol"], "INTC")
        self.assertEqual(request["request"]["action"], "demote")
        again = self.bridge.confirm("real-time", proposal["proposal_id"])
        self.assertTrue(again["already_submitted"])
        self.assertEqual(request["request"]["id"], again["request"]["id"])

    def test_overlay_exposes_synthetic_mode_without_changing_historical_default(self):
        controller = overlay_controller()
        controller.finish()
        initial_events = controller.snapshot().applied_event_count
        app = create_quote_dashboard(controller, synthetic_operator_root=self.root)
        elements = {getattr(c, "id", None): c for c in descendants(app.layout)}
        mode = elements["workspace-mode"]
        self.assertEqual(mode.value, "historical")
        real_time = next(x for x in mode.options if x["value"] == "real-time")
        self.assertFalse(real_time["disabled"])
        self.assertIn("operator-preview-button", elements)
        self.assertIn("operator-confirm-button", elements)
        self.assertEqual(controller.snapshot().applied_event_count, initial_events)

    def test_historical_only_dashboard_never_has_operator_buttons(self):
        app = create_quote_dashboard(overlay_controller())
        elements = {getattr(c, "id", None): c for c in descendants(app.layout)}
        real_time = next(x for x in elements["workspace-mode"].options if x["value"] == "real-time")
        self.assertTrue(real_time["disabled"])
        self.assertNotIn("operator-confirm-button", elements)


if __name__ == "__main__":
    unittest.main(verbosity=2)
