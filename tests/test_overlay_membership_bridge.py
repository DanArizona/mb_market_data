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


    def test_four_panels_sort_and_no_filter_row(self):
        app = create_quote_dashboard(overlay_controller(), synthetic_operator_root=self.root)
        components = {getattr(c, "id", None): c for c in descendants(app.layout)}
        panels = components["membership-tier-columns"].children
        self.assertEqual(len(panels), 4)
        self.assertEqual(panels[-1].id, "membership-lookup-column")
        self.assertEqual(panels[-1].children[0].id, "membership-search-panel")
        self.assertEqual(panels[-1].children[1].id, "membership-inspector-panel")
        self.assertIn("membership-search-symbol", components)
        self.assertIn("membership-find-button", components)
        self.assertEqual(
            [item.id for item in descendants(panels[-1].children[0])
             if getattr(item, "id", None) == "membership-search-symbol"],
            ["membership-search-symbol"],
        )
        for tier in ("uni", "focus", "hot"):
            grid = components[f"membership-{tier}-grid"]
            self.assertEqual([col["field"] for col in grid.columnDefs],
                             ["symbol", "last_price"])
            self.assertEqual(grid.columnDefs[0]["sort"], "asc")
            self.assertEqual(grid.columnDefs[0]["sortingOrder"], ["asc", "desc"])
            self.assertFalse(grid.defaultColDef["floatingFilter"])
            self.assertFalse(grid.defaultColDef["filter"])

    def test_global_find_selects_correct_tier_in_both_modes(self):
        controller = overlay_controller()
        controller.finish()
        app = create_quote_dashboard(controller, synthetic_operator_root=self.root)
        client = app.server.test_client()
        callback_key, callback = next(
            (key, entry) for key, entry in app.callback_map.items()
            if isinstance(entry["output"], list)
            and any(output.component_id == "membership-selected-symbol"
                    for output in entry["output"])
        )
        def search(symbol: str, mode: str):
            inputs = [
                {"id": item["id"], "property": item["property"],
                 "value": (1 if item["id"] == "membership-find-button" else
                           symbol if item["id"] == "membership-search-symbol"
                           and item["property"] == "value" else
                           0 if item["id"] == "membership-search-symbol" else
                           mode if item["id"] == "workspace-mode" else None)}
                for item in callback["inputs"]
            ]
            response = client.post("/_dash-update-component", json={
                "output": callback_key,
                "outputs": [{"id": output.component_id,
                             "property": output.component_property}
                            for output in callback["output"]],
                "changedPropIds": ["membership-find-button.n_clicks"],
                "inputs": inputs,
                "state": [
                    {"id": "workspace-mode", "property": "value", "value": mode},
                    {"id": "membership-selected-symbol", "property": "data",
                     "value": None},
                ],
            })
            self.addCleanup(response.close)
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            return response.get_json()["response"]

        synthetic = search("  intc ", "real-time")
        self.assertEqual(synthetic["membership-selected-symbol"]["data"], "INTC")
        self.assertEqual(synthetic["membership-hot-grid"]["selectedRows"],
                         {"ids": ["INTC"]})
        self.assertEqual(synthetic["membership-focus-grid"]["selectedRows"], [])
        self.assertIn("Hot", synthetic["membership-search-status"]["children"])
        self.assertEqual(synthetic["membership-search-symbol"]["value"], "INTC")
        self.assertEqual(synthetic["membership-hot-grid"]["scrollTo"],
                         {"rowId": "INTC", "rowPosition": "middle"})
        self.assertNotIn("scrollTo", synthetic.get("membership-focus-grid", {}))

        historical = search("spy", "historical")
        self.assertEqual(historical["membership-selected-symbol"]["data"], "SPY")
        self.assertEqual(historical["membership-focus-grid"]["selectedRows"],
                         {"ids": ["SPY"]})
        self.assertIn("Focus only", historical["membership-search-status"]["children"])
        self.assertEqual(historical["membership-search-symbol"]["value"], "SPY")
        self.assertEqual(historical["membership-focus-grid"]["scrollTo"],
                         {"rowId": "SPY", "rowPosition": "middle"})

        absent = search("NOTINSIDE", "real-time")
        self.assertIsNone(absent["membership-selected-symbol"]["data"])
        self.assertEqual(absent["membership-hot-grid"]["selectedRows"], [])
        self.assertIn("not in Uni, Focus, or Hot",
                      absent["membership-search-status"]["children"])
        self.assertNotIn("scrollTo", absent.get("membership-hot-grid", {}))

    def test_operator_dropdown_uses_theme_aware_menu_styles(self):
        css = (Path(__file__).resolve().parents[1] / "src" / "mb_market_data"
               / "assets" / "quote_dashboard.css").read_text(encoding="utf-8")
        self.assertIn(".operator-bridge-panel .Select-menu-outer", css)
        self.assertIn(".Select-menu-outer .Select-option.is-focused", css)
        self.assertIn("background: var(--surface-raised) !important", css)


    def test_editing_search_clears_stale_details_and_highlights(self):
        app = create_quote_dashboard(overlay_controller(), synthetic_operator_root=self.root)
        client = app.server.test_client()
        callback_key, callback = next(
            (key, entry) for key, entry in app.callback_map.items()
            if isinstance(entry["output"], list)
            and any(output.component_id == "membership-selected-symbol"
                    for output in entry["output"])
        )
        def change_search(value, prior):
            inputs = [
                {"id": item["id"], "property": item["property"],
                 "value": (value if item["id"] == "membership-search-symbol"
                           and item["property"] == "value" else
                           "real-time" if item["id"] == "workspace-mode" else
                           0 if item["id"] in ("membership-find-button",
                                                "membership-search-symbol") else None)}
                for item in callback["inputs"]
            ]
            response = client.post("/_dash-update-component", json={
                "output": callback_key,
                "outputs": [{"id": item.component_id,
                             "property": item.component_property}
                            for item in callback["output"]],
                "changedPropIds": ["membership-search-symbol.value"],
                "inputs": inputs,
                "state": [
                    {"id": "workspace-mode", "property": "value", "value": "real-time"},
                    {"id": "membership-selected-symbol", "property": "data",
                     "value": prior},
                ],
            })
            self.addCleanup(response.close)
            return response
        # Reflection after a row click must not clear the selected row.
        reflected = change_search("INTC", "INTC")
        self.assertEqual(reflected.status_code, 204)
        # Any user edit invalidates the currently inspected symbol.
        edited = change_search("INT", "INTC")
        self.assertEqual(edited.status_code, 200)
        updates = edited.get_json()["response"]
        self.assertIsNone(updates["membership-selected-symbol"]["data"])
        for tier in ("uni", "focus", "hot"):
            self.assertEqual(updates[f"membership-{tier}-grid"]["selectedRows"], [])
            self.assertNotIn("scrollTo", updates[f"membership-{tier}-grid"])
        self.assertIn("Press Find or Enter",
                      updates["membership-search-status"]["children"])

    def test_symbol_search_strip_and_scroll_are_independent_of_details(self):
        app = create_quote_dashboard(overlay_controller(), synthetic_operator_root=self.root)
        components = {getattr(c, "id", None): c for c in descendants(app.layout)}
        lookup = components["membership-lookup-column"]
        self.assertEqual([child.id for child in lookup.children],
                         ["membership-search-panel", "membership-inspector-panel"])
        search = components["membership-search-symbol"]
        self.assertEqual(search.placeholder, "Ticker")
        css = (Path(__file__).resolve().parents[1] / "src" / "mb_market_data"
               / "assets" / "quote_dashboard.css").read_text(encoding="utf-8")
        self.assertIn(".membership-lookup-column", css)
        self.assertIn("max-width: 8.5rem", css)
        # Stable row IDs rather than indices ensure scroll uses the displayed
        # sort order, including when the user reverses ascending to descending.
        for tier in ("uni", "focus", "hot"):
            grid = components[f"membership-{tier}-grid"]
            self.assertEqual(grid.dashGridOptions["getRowId"],
                             {"function": "params.data.symbol"})
            self.assertEqual(grid.columnDefs[0]["sortingOrder"], ["asc", "desc"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
