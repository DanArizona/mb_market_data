"""Offline tests for optional controller-owned OHLCV capture; no network."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mb_market_data.observation_overlay import ET
from mb_market_data.overlay_live_ohlcv import LiveOHLCVReader
from mb_market_data.overlay_ohlcv_worker import (
    OwnedOHLCVSelection, configure_ohlcv, owned_ohlcv_service,
)
from mb_market_data.daily_runtime import BackoffClient
from mb_market_data import daily_runtime as rt
import test_daily_runtime as runtime_tests
from test_overlay_live_ohlcv import Response, StubClient, raw_candle

UTC = timezone.utc
DAY = date(2026, 11, 25)


def et(hour, minute, second=0):
    return datetime(2026, 11, 25, hour, minute, second, tzinfo=ET)


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


class OwnedOHLCVWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = Path(self.tmp.name) / "market-data"
        data.mkdir()
        root = data / "output" / "daily_session" / str(DAY)
        root.mkdir(parents=True)
        self.root = root
        self.clock = Clock(et(9, 35, 5))
        self.plan = configure_ohlcv({
            "data_root": str(data),
            "state_root": str(root),
            "session_date": DAY.isoformat(),
            "opens_at": et(9, 30).isoformat(),
            "closes_at": et(16, 0).isoformat(),
            "opening_content_sha256": "a" * 64,
            "journal": str(data / "output" / "journal" / "2026-11-25.sqlite3"),
        })
        self.owner = 45678
        self.heartbeat()

    def heartbeat(self, pid=None):
        (self.root / "owner.json").write_text(
            json.dumps({
                "pid": self.owner if pid is None else pid,
                "at": self.clock().isoformat(),
                "active": True,
            }), encoding="utf-8",
        )

    def bridge(self, **kwargs):
        return OwnedOHLCVSelection(
            self.plan, owner_pid=self.owner, clock=self.clock, **kwargs,
        )

    def fixture(self):
        return StubClient({
            "symbol": "TEST",
            "candles": [raw_candle(9, 30, 100), raw_candle(9, 35, 200)],
        })

    def test_configuration_is_opt_in_and_path_bound(self):
        self.assertTrue(self.plan["overlay_ohlcv_enabled"])
        self.assertEqual(
            Path(self.plan["overlay_ohlcv_selection_root"]),
            self.root / "ohlcv",
        )
        self.assertEqual(
            Path(self.plan["overlay_ohlcv_evidence_root"]),
            Path(self.plan["data_root"]) / "output" / "overlay_ohlcv_live",
        )
        self.assertEqual(self.plan["overlay_ohlcv_request_budget"], 5)
        self.assertEqual(self.plan["overlay_ohlcv_request_budget_window_seconds"], 60)
        self.assertFalse((self.root / "ohlcv").exists())

    def test_new_and_legacy_plans_enforce_the_effective_minute_budget(self):
        symbols = ("TEST", "SECOND", "THIRD", "FOURTH", "FIFTH", "SIXTH")
        for minute, legacy, budget in ((35, False, 5), (40, True, 5), (45, False, 2)):
            with self.subTest(legacy=legacy, budget=budget):
                plan = dict(self.plan)
                if legacy:
                    plan.pop("overlay_ohlcv_request_budget_window_seconds")
                    plan["overlay_ohlcv_request_budget"] = 2
                else:
                    plan["overlay_ohlcv_request_budget"] = budget
                original_plan = dict(plan)
                self.clock.at = et(9, minute, 5)
                self.heartbeat()
                sink = OwnedOHLCVSelection(plan, owner_pid=self.owner, clock=self.clock)
                sink.publish(symbols[0])
                client = self.fixture()
                reports = []

                def sleep(_seconds):
                    reports.append(json.loads((self.root / "ohlcv" / "status.json").read_text()))
                    if len(reports) < len(symbols):
                        self.clock.at += timedelta(seconds=2)
                        self.heartbeat()
                        symbol = symbols[len(reports)]
                        sink.publish(symbol)
                        payload = client.response.json()
                        payload["symbol"] = symbol
                        client.response = Response(payload)

                owned_ohlcv_service(
                    plan, client, owner_pid=self.owner,
                    stop=lambda: len(reports) == len(symbols),
                    clock=self.clock, sleep=sleep,
                )
                self.assertEqual(len(client.requests), budget)
                self.assertEqual(
                    [report["state"] for report in reports],
                    ["PUBLISHED"] * budget + ["BUDGET_EXHAUSTED"] * (len(symbols) - budget),
                )
                self.assertTrue(all(report["request_budget"] == budget for report in reports))
                self.assertTrue(all(report["request_budget_window_seconds"] == 60 for report in reports))
                self.assertEqual(plan, original_plan)

    def test_unsupported_request_windows_fail_before_acquisition(self):
        for window in (0, 300, "60", 60.0, None):
            with self.subTest(window=window):
                plan = dict(self.plan, overlay_ohlcv_request_budget_window_seconds=window)
                client = self.fixture()
                with self.assertRaisesRegex(ValueError, "window must be 60 seconds"):
                    owned_ohlcv_service(
                        plan, client, owner_pid=self.owner, stop=lambda: True,
                        clock=self.clock,
                    )
                self.assertEqual(client.requests, [])

    def test_selection_handoff_contains_no_credentials(self):
        sink = self.bridge()
        path = sink.publish(" test ")
        self.assertEqual(sink.read(), "TEST")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["symbol"], "TEST")
        self.assertEqual(data["owner_pid"], self.owner)
        self.assertEqual(data["opening_content_sha256"], "a" * 64)
        text = path.read_text(encoding="utf-8").lower()
        self.assertNotIn("password", text)
        self.assertNotIn("access_token", text)
        self.assertNotIn("encrypted", text)

    def test_replaced_owner_blocks_new_selection(self):
        sink = self.bridge()
        sink.publish("TEST")
        self.heartbeat(pid=self.owner + 1)
        with self.assertRaisesRegex(ValueError, "authorized"):
            sink.publish("MSFT")
        self.assertEqual(sink.read(), "TEST")

    def test_stop_and_outside_session_block_selection(self):
        sink = self.bridge()
        (self.root / "stop.request").write_text("stop", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "authorized"):
            sink.publish("TEST")
        (self.root / "stop.request").unlink()
        self.clock.at = et(16, 1)
        with self.assertRaisesRegex(ValueError, "outside"):
            sink.publish("TEST")
        self.assertFalse((self.root / "ohlcv").exists())

    def test_mismatched_selection_identity_and_symbol_are_ignored(self):
        sink = self.bridge()
        path = sink.publish("TEST")
        value = json.loads(path.read_text())
        value["owner_pid"] = self.owner + 1
        path.write_text(json.dumps(value))
        self.assertIsNone(sink.read())
        value["owner_pid"] = self.owner
        value["opening_content_sha256"] = "b" * 64
        path.write_text(json.dumps(value))
        self.assertIsNone(sink.read())
        value["opening_content_sha256"] = "a" * 64
        value["symbol"] = "../BADDATA"
        path.write_text(json.dumps(value))
        self.assertIsNone(sink.read())

    def test_owned_worker_publishes_completed_snapshots_and_exits(self):
        self.bridge().publish("TEST")
        client = self.fixture()
        calls = [0]

        def stop():
            return calls[0] >= 2

        def sleep(seconds):
            self.assertEqual(seconds, 2)
            calls[0] += 1
            self.clock.at += timedelta(minutes=5)
            self.heartbeat()

        code = owned_ohlcv_service(
            self.plan, client, owner_pid=self.owner, stop=stop,
            clock=self.clock, sleep=sleep,
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(client.requests[0][1]["endDate"], et(9, 35, 5))
        self.assertEqual(client.requests[1][1]["endDate"], et(9, 40, 5))
        reader = LiveOHLCVReader(
            self.plan["overlay_ohlcv_evidence_root"], session_date=DAY,
        )
        last = reader.latest("TEST", now=et(9, 45).astimezone(UTC))
        self.assertEqual([item.volume for item in last.candles], [100, 200])
        status = json.loads((self.root / "ohlcv" / "status.json").read_text())
        self.assertEqual(status["state"], "PUBLISHED")
        self.assertEqual(status["owner_pid"], self.owner)
        self.assertEqual(status["candle_count"], 2)
        self.assertEqual(status["symbol"], "TEST")
        self.assertEqual(status["pid"], os.getpid())
        self.assertEqual(status["last_success"]["latest_candle_end_et"], et(9, 40).isoformat())

    def test_restart_command_routes_to_owned_request_without_prompt(self):
        request = self.root / "restart-ohlcv.request"
        with patch.object(rt, "owner_available", return_value=True), \
                patch("getpass.getpass", side_effect=AssertionError("password prompt")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(rt.main(["restart-ohlcv", "--state-root", str(self.root)]), 0)
        self.assertTrue(request.is_file())
        request.unlink()
        with patch.object(rt, "owner_available", return_value=False):
            with self.assertRaisesRegex(ValueError, "fresh active controller"):
                rt.main(["restart-ohlcv", "--state-root", str(self.root)])
        self.assertFalse(request.exists())
        (self.root / "stop.request").write_text("stopped", encoding="utf-8")
        with patch.object(rt, "owner_available", return_value=True):
            with self.assertRaisesRegex(ValueError, "Collection was stopped"):
                rt.main(["restart-ohlcv", "--state-root", str(self.root)])
        self.assertFalse(request.exists())

    def test_last_success_survives_waiting_and_failed_request(self):
        self.bridge().publish("TEST")
        client = self.fixture()
        reports = []

        def sleep(_seconds):
            reports.append(json.loads((self.root / "ohlcv" / "status.json").read_text()))
            if len(reports) == 1:
                self.clock.at += timedelta(seconds=2)
            elif len(reports) == 2:
                self.clock.at = et(9, 40, 5)
                client.response.ok = False
                client.response.status_code = 503
            else:
                self.clock.at += timedelta(seconds=2)
            self.heartbeat()

        owned_ohlcv_service(
            self.plan, client, owner_pid=self.owner, stop=lambda: len(reports) >= 4,
            clock=self.clock, sleep=sleep,
        )
        self.assertEqual([r["state"] for r in reports],
                         ["PUBLISHED", "ALREADY_ATTEMPTED", "FAILED", "ALREADY_ATTEMPTED"])
        saved = reports[0]["last_success"]
        self.assertEqual(saved["latest_candle_end_et"], et(9, 35).isoformat())
        self.assertEqual(saved["acquired_at_utc"], et(9, 35, 5).astimezone(UTC).isoformat())
        self.assertTrue(all(r["last_success"] == saved for r in reports))
        self.assertTrue(all(r["pid"] == os.getpid() for r in reports))
        self.assertEqual(reports[-1]["last_attempt"]["state"], "FAILED")
        self.assertEqual(reports[-1]["last_attempt"]["error_type"], "ObservationOverlayRequestError")
        self.assertEqual(len(client.requests), 2)

    def test_changed_selection_never_relabels_saved_success(self):
        self.bridge().publish("TEST")
        client = self.fixture()
        reports = []

        def sleep(_seconds):
            reports.append(json.loads((self.root / "ohlcv" / "status.json").read_text()))
            self.clock.at += timedelta(seconds=2)
            self.heartbeat()
            if len(reports) == 1:
                self.bridge().publish("OTHER")
                client.response.ok = False
                client.response.status_code = 503
            elif len(reports) == 2:
                self.bridge().file.unlink()

        owned_ohlcv_service(
            self.plan, client, owner_pid=self.owner, stop=lambda: len(reports) >= 3,
            clock=self.clock, sleep=sleep,
        )
        self.assertEqual([r["symbol"] for r in reports], ["TEST", "OTHER", None])
        self.assertEqual([r["state"] for r in reports], ["PUBLISHED", "FAILED", "NO_SELECTION"])
        self.assertTrue(all(r["last_success"]["symbol"] == "TEST" for r in reports))
        self.assertEqual(reports[1]["last_attempt"]["symbol"], "OTHER")
        display = rt.readable_status({
            "session_date": str(DAY), "at": self.clock().isoformat(), "pid": self.owner,
            "phase": "COLLECTING", "services": {
                "ohlcv": {"pid": os.getpid(), "exit_code": None, "status": reports[1]},
            },
        })
        self.assertIn("Selected symbol: OTHER", display)
        self.assertIn("Last saved candles: TEST", display)
        self.assertIn("different selection", display)
        self.assertIn("OTHER FAILED", display)

    def test_controller_accepts_only_current_childs_actual_worker_status(self):
        for wrong_pid in (False, True):
            with self.subTest(wrong_pid=wrong_pid):
                fixture = runtime_tests.RuntimeTests()
                self.addCleanup(fixture.doCleanups)
                make_plan = runtime_tests.make_plan

                def plan_with_fixed_calendar(*args, **kwargs):
                    def calendar(day):
                        opening = datetime(day.year, day.month, day.day, 9, 30, tzinfo=ET)
                        return runtime_tests.SessionSchedule(
                            day, opening, opening.replace(hour=16),
                            day + timedelta(days=1), "offline-fixture",
                        )
                    return make_plan(*args, schedule_resolver=calendar, **kwargs)

                with patch.object(runtime_tests, "make_plan", side_effect=plan_with_fixed_calendar):
                    fixture.setUp()
                fixture.plan = configure_ohlcv(fixture.plan)

                def produce_status(child_pid):
                    fixture.clock.now = fixture.clock().replace(minute=35, second=5)
                    worker_clock = Clock(fixture.clock())
                    owner_pid = os.getpid()
                    selection = OwnedOHLCVSelection(
                        fixture.plan, owner_pid=owner_pid, clock=worker_clock,
                        owner_check=lambda *_: True,
                    )
                    selection.publish("TEST")
                    candle = raw_candle(9, 30, 100)
                    candle["datetime"] = int(worker_clock().replace(minute=30, second=0).timestamp() * 1000)
                    client = StubClient({"symbol": "TEST", "candles": [candle]})
                    finished = []
                    with patch("mb_market_data.overlay_ohlcv_worker.os.getpid", return_value=child_pid):
                        owned_ohlcv_service(
                            fixture.plan, client, owner_pid=owner_pid,
                            stop=lambda: bool(finished), clock=worker_clock,
                            sleep=lambda _: finished.append(True),
                        )
                    status = json.loads((fixture.state / "ohlcv" / "status.json").read_text())
                    if wrong_pid:
                        status["pid"] += 1
                    return status

                launched, _, result = fixture.exercise_controller(
                    close_minute=40, ohlcv_status=produce_status,
                )
                self.assertIn("ohlcv", launched)
                status = result["services"]["ohlcv"]["status"]
                if wrong_pid:
                    self.assertEqual(status, {})
                else:
                    self.assertEqual(status["pid"], result["services"]["ohlcv"]["pid"])
                    self.assertEqual(status["last_success"]["symbol"], "TEST")
                    latest = fixture.clock().replace(hour=9, minute=35, second=0).isoformat()
                    self.assertIn("latest completed " + latest, rt.readable_status(result))

    def test_stop_request_prevents_provider_calls(self):
        self.bridge().publish("TEST")
        (self.root / "stop.request").write_text("stop", encoding="utf-8")
        client = self.fixture()
        code = owned_ohlcv_service(
            self.plan, client, owner_pid=self.owner,
            stop=lambda: False, clock=self.clock, sleep=lambda _: None,
        )
        self.assertEqual(code, 0)
        self.assertEqual(client.requests, [])

    def test_price_history_backoff_avoids_repeated_rate_limited_calls(self):
        client = self.fixture()
        client.response.status_code = 429
        client.response.headers = {"Retry-After": "60"}
        client.response.ok = False
        current = [0.0]
        wrapper = BackoffClient(client, clock=lambda: current[0])
        response = wrapper.price_history("TEST", frequency=5)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(wrapper.retry_at, 60.0)
        with self.assertRaisesRegex(RuntimeError, "provider backoff"):
            wrapper.price_history("TEST", frequency=5)
        self.assertEqual(len(client.requests), 1)


if __name__ == "__main__":
    unittest.main()
