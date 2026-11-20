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
        self.assertEqual(
            [panel.id for panel in panels[-1].children],
            ["membership-search-panel", "membership-outside-panel",
             "membership-inspector-panel"],
        )
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
        self.assertIsNone(synthetic["membership-admission-candidate"]["data"])
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
        self.assertEqual(absent["membership-admission-candidate"]["data"], "NOTINSIDE")
        malformed = search("$$", "real-time")
        self.assertIsNone(malformed["membership-admission-candidate"]["data"])
        self.assertIn("Invalid canonical ticker format",
                      malformed["membership-search-status"]["children"])
        historical_absent = search("AMD", "historical")
        self.assertIsNone(historical_absent["membership-admission-candidate"]["data"])

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
        self.assertIsNone(updates["membership-admission-candidate"]["data"])

    def test_symbol_search_strip_and_scroll_are_independent_of_details(self):
        app = create_quote_dashboard(overlay_controller(), synthetic_operator_root=self.root)
        components = {getattr(c, "id", None): c for c in descendants(app.layout)}
        lookup = components["membership-lookup-column"]
        self.assertEqual([child.id for child in lookup.children],
                         ["membership-search-panel", "membership-outside-panel",
                          "membership-inspector-panel"])
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


    def test_drag_backend_proposes_demote_and_promote_without_writing(self):
        journal = Path(self.demo.plan["journal"])
        baseline = journal.read_bytes()
        inbox = self.root / "membership" / "inbox"

        demote = self.bridge.preview_drop("real-time", "INTC", "hot", "uni")
        self.assertEqual((demote["symbol"], demote["action"], demote["target"]),
                         ("INTC", "demote", "uni"))
        self.assertFalse(demote["request_written"])
        promote = self.bridge.preview_drop("real-time", "AAME", "uni", "hot")
        self.assertEqual((promote["symbol"], promote["action"], promote["target"]),
                         ("AAME", "promote", "hot"))
        focus_to_hot = self.bridge.preview_drop("real-time", "ACH", "focus", "hot")
        self.assertEqual(focus_to_hot["action"], "promote")
        self.assertEqual(journal.read_bytes(), baseline)
        self.assertFalse(list(inbox.glob("*.json")), "Preview must never queue an inbox request")

    def test_drag_rejects_historical_spoofed_source_and_same_tier(self):
        with self.assertRaisesRegex(OperatorProblem, "read-only"):
            self.bridge.preview_drop("historical", "INTC", "hot", "focus")
        with self.assertRaisesRegex(OperatorProblem, "Source tier changed"):
            self.bridge.preview_drop("real-time", "INTC", "focus", "uni")
        with self.assertRaisesRegex(OperatorProblem, "between different"):
            self.bridge.preview_drop("real-time", "INTC", "hot", "hot")
        with self.assertRaisesRegex(OperatorProblem, "between different"):
            self.bridge.preview_drop("real-time", "INTC", "hot", "outside")
        with self.assertRaisesRegex(OperatorProblem, "Source tier changed"):
            self.bridge.preview_drop("real-time", "ZZZZ", "hot", "focus")

    def test_synthetic_grid_registers_only_preview_first_dropzones(self):
        app = create_quote_dashboard(overlay_controller(), synthetic_operator_root=self.root)
        components = {getattr(c, "id", None): c for c in descendants(app.layout)}
        for tier in ("uni", "focus", "hot"):
            grid = components[f"membership-{tier}-grid"]
            self.assertTrue(grid.columnDefs[0]["rowDrag"])
            self.assertTrue(grid.dashGridOptions["suppressRowDrag"])
            self.assertTrue(grid.dashGridOptions["suppressMoveWhenRowDragging"])
            self.assertTrue(grid.dashGridOptions["rowDragManaged"])
            self.assertFalse(grid.dashGridOptions["rowDragMultiRow"])
            self.assertIsNotNone(components[f"membership-tier-{tier}"])
        self.assertIn("membership-drag-request", components)
        self.assertIn("membership-drag-binding-status", components)
        self.assertIn("membership-drag-status", components)
        self.assertIn("membership-drag-binding-status.data", app.callback_map)
        js_path = (Path(__file__).resolve().parents[1] / "src" / "mb_market_data"
                   / "assets" / "membership_drag_bridge.js")
        script = js_path.read_text(encoding="utf-8")
        self.assertIn("addRowDropZone", script)
        self.assertIn("removeRowDropZone", script)
        self.assertIn("dash_ag_grid.getApiAsync", script)
        self.assertIn('set_props("membership-drag-request"', script)
        self.assertNotIn("applyTransaction", script)

    def test_historical_dashboard_has_no_drag_submission_surface(self):
        app = create_quote_dashboard(overlay_controller())
        elements = {getattr(c, "id", None): c for c in descendants(app.layout)}
        self.assertNotIn("membership-drag-request", elements)
        self.assertNotIn("membership-drag-binding-status", elements)
        self.assertNotIn("membership-admission-candidate", elements)
        self.assertNotIn("membership-admit-button", elements)
        self.assertNotIn("membership-outside-panel", elements)
        for tier in ("uni", "focus", "hot"):
            grid = elements[f"membership-{tier}-grid"]
            self.assertFalse(grid.columnDefs[0]["rowDrag"])
            self.assertTrue(grid.dashGridOptions["suppressRowDrag"])

    def test_drag_event_dash_preview_callback_never_publishes(self):
        app = create_quote_dashboard(overlay_controller(), synthetic_operator_root=self.root)
        client = app.server.test_client()
        callback_key, callback = next(
            (key, entry) for key, entry in app.callback_map.items()
            if isinstance(entry["output"], list)
            and any(output.component_id == "operator-preview-proposal"
                    for output in entry["output"])
        )
        self.assertIn(("membership-drag-request", "data"),
                      [(arg["id"], arg["property"]) for arg in callback["inputs"]])
        before = Path(self.demo.plan["journal"]).read_bytes()
        inbox = self.root / "membership" / "inbox"

        def drop(mode, from_tier):
            payload = {"symbol": "INTC", "source_tier": from_tier,
                       "destination_tier": "focus", "nonce": "synthetic-test-123"}
            inputs = [
                {"id": item["id"], "property": item["property"],
                 "value": (payload if item["id"] == "membership-drag-request"
                           else mode if item["id"] == "workspace-mode" else 0)}
                for item in callback["inputs"]
            ]
            state = [
                {"id": item["id"], "property": item["property"],
                 "value": ("INTC" if item["id"] == "operator-symbol" else
                           "demote" if item["id"] == "operator-action" else "focus")}
                for item in callback["state"]
            ]
            response = client.post("/_dash-update-component", json={
                "output": callback_key,
                "outputs": [{"id": item.component_id, "property": item.component_property}
                            for item in callback["output"]],
                "changedPropIds": ["membership-drag-request.data"],
                "inputs": inputs, "state": state,
            })
            self.addCleanup(response.close)
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            return response.get_json()["response"]

        result = drop("real-time", "hot")
        proposed = result["operator-preview-proposal"]["data"]
        self.assertEqual((proposed["action"], proposed["target"]), ("demote", "focus"))
        self.assertFalse(proposed["request_written"])
        self.assertIn("DRAG PREVIEW ONLY", result["operator-preview-status"]["children"])
        blocked = drop("historical", "hot")
        self.assertIsNone(blocked["operator-preview-proposal"]["data"])
        self.assertIn("read-only", blocked["operator-preview-status"]["children"])
        spoofed = drop("real-time", "uni")
        self.assertIsNone(spoofed["operator-preview-proposal"]["data"])
        self.assertIn("Source tier changed", spoofed["operator-preview-status"]["children"])
        self.assertEqual(Path(self.demo.plan["journal"]).read_bytes(), before)
        self.assertFalse(list(inbox.glob("*.json")))


    def test_outside_admission_preview_is_unverified_and_nonmutating(self):
        journal = Path(self.demo.plan["journal"])
        before = journal.read_bytes()
        inbox = self.root / "membership" / "inbox"
        proposal = self.bridge.preview_admission("real-time", " amd ")
        self.assertEqual(
            (proposal["symbol"], proposal["before"], proposal["after"],
             proposal["action"], proposal["target"]),
            ("AMD", "outside", "uni", "promote", "uni"),
        )
        self.assertFalse(proposal["request_written"])
        self.assertIn("identity validation", proposal["note"])
        self.assertEqual(journal.read_bytes(), before)
        self.assertFalse(list(inbox.glob("*.json")))

    def test_outside_admission_rejects_member_syntax_and_historical_mode(self):
        with self.assertRaisesRegex(OperatorProblem, "read-only"):
            self.bridge.preview_admission("historical", "AMD")
        with self.assertRaisesRegex(OperatorProblem, "canonical security"):
            self.bridge.preview_admission("real-time", "$$")
        with self.assertRaisesRegex(OperatorProblem, "already in HOT"):
            self.bridge.preview_admission("real-time", "INTC")
        with self.assertRaisesRegex(OperatorProblem, "already in UNI"):
            self.bridge.preview_admission("real-time", "AAME")

    def test_outside_admission_card_only_ready_for_current_synthetic_nonmember(self):
        app = create_quote_dashboard(overlay_controller(), synthetic_operator_root=self.root)
        client = app.server.test_client()
        components = {getattr(c, "id", None): c for c in descendants(app.layout)}
        self.assertEqual(components["membership-outside-panel"].style,
                         {"display": "none"})
        self.assertIn("membership-admit-button", components)
        self.assertIn("membership-admission-candidate", components)
        callback_key, callback = next(
            (key, entry) for key, entry in app.callback_map.items()
            if isinstance(entry["output"], list)
            and any(item.component_id == "membership-outside-panel"
                    for item in entry["output"])
        )
        def view(candidate, mode):
            response = client.post("/_dash-update-component", json={
                "output": callback_key,
                "outputs": [{"id": item.component_id, "property": item.component_property}
                            for item in callback["output"]],
                "changedPropIds": ["membership-admission-candidate.data"],
                "inputs": [
                    {"id": i["id"], "property": i["property"],
                     "value": (candidate if i["id"] == "membership-admission-candidate"
                               else mode if i["id"] == "workspace-mode" else 0)}
                    for i in callback["inputs"]
                ],
                "state": [],
            })
            self.addCleanup(response.close)
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            return response.get_json()["response"]
        allowed = view("AMD", "real-time")
        self.assertEqual(allowed["membership-outside-panel"]["style"], {})
        self.assertFalse(allowed["membership-admit-button"]["disabled"])
        self.assertIn("AMD — outside", allowed["membership-admission-label"]["children"])
        self.assertEqual(view("AMD", "historical")
                         ["membership-outside-panel"]["style"], {"display": "none"})
        self.assertEqual(view("INTC", "real-time")
                         ["membership-outside-panel"]["style"], {"display": "none"})

    def test_admission_button_creates_exact_promotion_preview_and_never_queues(self):
        app = create_quote_dashboard(overlay_controller(), synthetic_operator_root=self.root)
        client = app.server.test_client()
        callback_key, callback = next(
            (key, entry) for key, entry in app.callback_map.items()
            if isinstance(entry["output"], list)
            and any(item.component_id == "operator-preview-proposal"
                    for item in entry["output"])
        )
        before = Path(self.demo.plan["journal"]).read_bytes()
        inbox = self.root / "membership" / "inbox"
        def click(candidate, search, mode="real-time"):
            inputs = [
                {"id": i["id"], "property": i["property"],
                 "value": (1 if i["id"] == "membership-admit-button"
                           else search if i["id"] == "membership-search-symbol"
                           else mode if i["id"] == "workspace-mode" else 0)}
                for i in callback["inputs"]
            ]
            state = [
                {"id": i["id"], "property": i["property"],
                 "value": (candidate if i["id"] == "membership-admission-candidate"
                           else "INTC" if i["id"] == "operator-symbol"
                           else "demote" if i["id"] == "operator-action"
                           else "hot")}
                for i in callback["state"]
            ]
            response = client.post("/_dash-update-component", json={
                "output": callback_key,
                "outputs": [{"id": i.component_id, "property": i.component_property}
                            for i in callback["output"]],
                "changedPropIds": ["membership-admit-button.n_clicks"],
                "inputs": inputs, "state": state,
            })
            self.addCleanup(response.close)
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            return response.get_json()["response"]
        proposed = click("AMD", "amd")
        data = proposed["operator-preview-proposal"]["data"]
        self.assertEqual((data["symbol"], data["action"], data["target"], data["before"]),
                         ("AMD", "promote", "uni", "outside"))
        self.assertFalse(data["request_written"])
        self.assertIn("ADMISSION PREVIEW ONLY",
                      proposed["operator-preview-status"]["children"])
        mismatch = click("AMD", "MSFT")
        self.assertIsNone(mismatch["operator-preview-proposal"]["data"])
        self.assertIn("search again", mismatch["operator-preview-status"]["children"])
        historical = click("AMD", "AMD", "historical")
        self.assertIsNone(historical["operator-preview-proposal"]["data"])
        self.assertIn("read-only", historical["operator-preview-status"]["children"])
        already = click("INTC", "INTC")
        self.assertIsNone(already["operator-preview-proposal"]["data"])
        self.assertIn("already in HOT",
                      already["operator-preview-status"]["children"])
        self.assertEqual(Path(self.demo.plan["journal"]).read_bytes(), before)
        self.assertFalse(list(inbox.glob("*.json")))

    def test_admission_button_sets_existing_operator_controls(self):
        app = create_quote_dashboard(overlay_controller(), synthetic_operator_root=self.root)
        client = app.server.test_client()
        callback_key, callback = next(
            (key, entry) for key, entry in app.callback_map.items()
            if isinstance(entry["output"], list)
            and any(item.component_id == "operator-action" for item in entry["output"])
        )
        response = client.post("/_dash-update-component", json={
            "output": callback_key,
            "outputs": [{"id": i.component_id, "property": i.component_property}
                        for i in callback["output"]],
            "changedPropIds": ["membership-admit-button.n_clicks"],
            "inputs": [
                {"id": i["id"], "property": i["property"],
                 "value": (1 if i["id"] == "membership-admit-button"
                           else "real-time" if i["id"] == "workspace-mode" else None)}
                for i in callback["inputs"]
            ],
            "state": [
                {"id": i["id"], "property": i["property"],
                 "value": ("real-time" if i["id"] == "workspace-mode" else
                           "AMD" if i["id"] == "membership-admission-candidate" else
                           "amd")}
                for i in callback["state"]
            ],
        })
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        changes = response.get_json()["response"]
        self.assertEqual(changes["operator-symbol"]["value"], "AMD")
        self.assertEqual(changes["operator-action"]["value"], "promote")
        self.assertEqual(changes["operator-target"]["value"], "uni")

    def test_outside_admission_real_worker_effective_and_simulated_rejection(self):
        original = self.bridge.snapshot()
        proposal = self.bridge.preview_admission("real-time", "AMD")
        request = self.bridge.confirm("real-time", proposal["proposal_id"])
        self.assertEqual(request["request"]["action"], "promote")
        self.assertEqual(request["request"]["target"], "uni")
        cid = request["request"]["id"]
        result = None
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            result = self.bridge.result(cid)
            if result["status"] == "EFFECTIVE_AS_PUBLISHED":
                break
            self.assertNotIn(result["status"], ("REJECTED", "FAILED", "NOT_FOUND"))
            time.sleep(.1)
        self.assertEqual(result["status"], "EFFECTIVE_AS_PUBLISHED")
        current = self.bridge.snapshot("AMD")
        self.assertEqual(current["selected"]["tier"], "uni")
        self.assertEqual(current["counts"]["uni"], original["counts"]["uni"] + 1)
        self.assertEqual(current["counts"]["focus"], original["counts"]["focus"])
        self.assertEqual(current["counts"]["hot"], original["counts"]["hot"])
        self.assertIn("AMD", [r["symbol"] for r in self.bridge.records()["uni"]])

        # Canonical-looking NOTREAL reaches the synthetic identity fixture
        # but is explicitly refused: ticker syntax alone is not equity identity.
        negative = self.bridge.preview_admission("real-time", "NOTREAL")
        rejected_command = self.bridge.confirm("real-time", negative["proposal_id"])
        negative_id = rejected_command["request"]["id"]
        rejected = None
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            rejected = self.bridge.result(negative_id)
            if rejected["status"] == "REJECTED":
                break
            self.assertNotIn(rejected["status"], ("FAILED", "NOT_FOUND"))
            time.sleep(.1)
        self.assertEqual(rejected["status"], "REJECTED")
        self.assertIn("SIMULATED: Exact equity identity not confirmed",
                      rejected["message"])
        self.assertEqual(self.bridge.snapshot("NOTREAL")["selected"]["tier"],
                         "outside")


if __name__ == "__main__":
    unittest.main(verbosity=2)
