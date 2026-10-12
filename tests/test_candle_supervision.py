from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from threading import Event
import time
import unittest
from unittest.mock import patch

from mb_market_data.candle_capture import preserve, decode_candles, validate_window
from mb_market_data.candle_receipts import CandleReceiptReader, CandleReceiptError
from mb_market_data.candle_session import CandleSessionReader
from mb_market_data.candle_supervision import (
    CandleHealth, DirectoryLock, health_view, publish_json, stamp,
)
from mb_market_data.quote_dashboard import create_quote_dashboard
from mb_market_data.quote_dashboard_live import QuoteDashboardLiveController
from mb_market_data.quote_journal_live import QuoteJournalLiveReader
from test_candle_receipts import capture_plan, write_receipt
from test_quote_journal_live import LiveFixture, BOUNDARY, SESSION
from test_live_overlay_dashboard import request_callback

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]


class HealthFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plan = {"session_id": "test-session", "symbol": "TEST", "session_date": str(SESSION),
                     "interval_seconds": 15, "http_timeout_seconds": 10}
        publish_json(self.root / "session_plan.json", self.plan)
        self.run = self.root / "run_0001"
        capture_plan(self.run)
        self.reader = CandleReceiptReader(self.run)
        self.monitor = CandleHealth(self.root, self.plan)
        self.phase = {"state": "RUNNING", "started_at_utc": stamp(BOUNDARY)}

    def inspect(self, seconds=3, **kw):
        self.reader.refresh()
        return self.monitor.inspect(run_id=kw.pop("run_id", "run_0001"),
            phase=kw.pop("phase", self.phase), reader=kw.pop("reader", self.reader),
            now=BOUNDARY + timedelta(seconds=seconds), alive=kw.pop("alive", True), **kw)


class TestCandleHealth(HealthFixture):
    def test_unchanged_values_with_new_receipts_remain_healthy(self):
        write_receipt(self.run, 1, BOUNDARY+timedelta(seconds=1))
        self.assertEqual(self.inspect()["state"], "HEALTHY")
        write_receipt(self.run, 2, BOUNDARY+timedelta(seconds=30))
        self.assertEqual(self.inspect(40)["state"], "HEALTHY")
        self.assertEqual(self.monitor.events, [])

    def test_api_error_is_quiet_and_keeps_last_success(self):
        write_receipt(self.run, 1, BOUNDARY+timedelta(seconds=1))
        self.inspect()
        write_receipt(self.run, 2, BOUNDARY+timedelta(seconds=15), outcome="HTTP_ERROR")
        value = self.inspect(16)
        self.assertEqual(value["state"], "WARNING")
        self.assertFalse(value["audible_alarm"])
        self.assertEqual(value["severity"], "warning")
        self.assertEqual(value["last_successful_acquisition_at_utc"], stamp(BOUNDARY+timedelta(seconds=1)))
        self.assertEqual(len(self.monitor.events), 1)
        self.inspect(20)
        self.assertEqual(len(self.monitor.events), 1)

    def test_overdue_and_abrupt_exit_are_visible_without_new_requests(self):
        self.assertEqual(self.inspect(36)["state"], "WARNING")
        self.assertIn("expected request interval", self.inspect(40)["message"])
        dead = self.inspect(41, alive=False)
        self.assertIn("exited unexpectedly", dead["message"])
        self.assertEqual(len(self.monitor.events), 1)

    def test_waiting_for_credentials_is_not_an_acquisition_failure(self):
        waiting = {**self.phase, "state": "WAITING_FOR_CREDENTIALS"}
        self.assertEqual(self.inspect(200, phase=waiting)["state"], "WAITING_FOR_CREDENTIALS")
        self.assertEqual(self.monitor.events, [])

    def test_replacement_launch_does_not_clear_failure_until_new_success(self):
        self.inspect(3, alive=False)
        second = self.root / "run_0002"
        capture_plan(second)
        reader = CandleReceiptReader(second)
        phase = {"state": "RUNNING", "started_at_utc": stamp(BOUNDARY+timedelta(seconds=10))}
        pending = self.inspect(10, run_id=second.name, reader=reader, phase=phase)
        self.assertEqual(pending["state"], "RECOVERY_PENDING")
        write_receipt(second, 1, BOUNDARY+timedelta(seconds=11))
        reader.refresh()
        restored = self.inspect(12, run_id=second.name, reader=reader, phase=phase)
        self.assertEqual(restored["state"], "HEALTHY")
        event = self.monitor.events[-1]
        self.assertEqual(event["kind"], "INPUT_RESTORED")
        self.assertEqual(event["recovery_acquisition_at_utc"], stamp(BOUNDARY+timedelta(seconds=11)))
        self.assertEqual(event["backfill_status"], "NEEDS_REVIEW")

    def test_failed_history_publication_is_retained_and_retried(self):
        with patch("mb_market_data.candle_supervision.publish_json", side_effect=PermissionError()):
            failed = self.inspect(3, alive=False)
        self.assertEqual(failed["state"], "WARNING")
        self.assertTrue(self.monitor.pending_events)
        self.inspect(4, alive=False)
        self.assertEqual(self.monitor.pending_events, [])
        self.assertEqual(len(list((self.root / "health_events").glob("*.json"))), 1)

    def test_corrupt_evidence_cannot_report_healthy(self):
        write_receipt(self.run, 1, BOUNDARY+timedelta(seconds=1))
        self.inspect()
        value = self.inspect(5, reader_error="hash mismatch")
        self.assertEqual(value["state"], "WARNING")

    def test_normal_completion_and_operator_stop_are_not_failures(self):
        for state in ("DURATION_COMPLETE", "STOPPED_BY_OPERATOR", "WINDOW_ENDED"):
            value = self.inspect(5, alive=False, phase={**self.phase, "state": state})
            self.assertEqual(value["state"], state)
        self.assertEqual(self.monitor.events, [])

    def test_independent_reader_detects_stale_supervisor_and_watchdog(self):
        value = self.inspect()
        publish_json(self.root / "supervisor_status.json", value)
        watcher = {"session_id": self.plan["session_id"], "heartbeat_at_utc": stamp(BOUNDARY+timedelta(seconds=3))}
        publish_json(self.root / "watchdog_status.json", watcher)
        current = BOUNDARY+timedelta(seconds=4)
        self.assertEqual(health_view(self.root, now=current)["state"], value["state"])
        stale = health_view(self.root, now=current+timedelta(seconds=20))
        self.assertIn("supervisor heartbeat is stale", stale["message"])
        publish_json(self.root / "supervisor_status.json", {**value, "heartbeat_at_utc": stamp(current+timedelta(seconds=20))}, replace=True)
        self.assertIn("watchdog heartbeat is stale", health_view(self.root, now=current+timedelta(seconds=20))["message"])

    def test_unreadable_health_is_warning_and_has_no_acquisition_side_effect(self):
        before = self.run.joinpath("request_plan.json").read_bytes()
        self.assertEqual(health_view(self.root)["state"], "WARNING")
        self.assertEqual(before, self.run.joinpath("request_plan.json").read_bytes())

    def test_closed_session_does_not_require_an_ongoing_heartbeat(self):
        value={**self.inspect(),"state":"SESSION_CLOSED","message":"Session closed","severity":"notice"}
        publish_json(self.root/"supervisor_status.json",value)
        self.assertEqual(health_view(self.root,now=BOUNDARY+timedelta(hours=1))["state"],"SESSION_CLOSED")


class TestEvidencePublication(HealthFixture):
    def test_temporary_cleanup_denial_does_not_undo_a_committed_receipt(self):
        path=self.root/"complete.json"
        with patch("mb_market_data.candle_supervision.Path.unlink",side_effect=PermissionError("synthetic reader")):
            publish_json(path,{"complete":True})
        self.assertEqual(json.loads(path.read_text()),{"complete":True})

    def test_receipt_is_invisible_until_atomic_commit_and_cannot_be_replaced(self):
        original = os.link
        observed = []
        def link(source, target):
            if Path(target).name.endswith("receipt.json"):
                self.reader.refresh()
                observed.append(self.reader.receipt_count)
            return original(source, target)
        when = BOUNDARY+timedelta(seconds=1)
        body = json.dumps({"symbol":"TEST", "candles":[{"datetime":int((BOUNDARY-timedelta(minutes=5)).timestamp()*1000),
            "open":100,"high":101,"low":99,"close":100,"volume":50}]}).encode()
        receipt = {"sample_number":1,"outcome":"OK","http_status":200,
                   "received_at_utc":stamp(when),"dispatched_at_utc":stamp(when-timedelta(seconds=.1))}
        with patch("mb_market_data.candle_supervision.os.link", side_effect=link):
            preserve(self.run, receipt, body)
        self.assertEqual(observed, [0])
        self.reader.refresh()
        self.assertEqual(self.reader.receipt_count, 1)
        path = self.run / "sample_0001.receipt.json"
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            publish_json(path, {"changed":True})
        self.assertEqual(path.read_bytes(), before)

    def test_windows_sharing_denial_retries_without_removing_previous_status(self):
        path = self.root / "status.json"
        publish_json(path, {"old":True})
        original = os.replace
        calls = []
        def replace(source, target):
            calls.append(1)
            if len(calls) < 3:
                self.assertEqual(json.loads(path.read_text()), {"old":True})
                raise PermissionError(errno.EACCES, "synthetic sharing denial")
            return original(source, target)
        with patch("mb_market_data.candle_supervision.os.replace", side_effect=replace):
            publish_json(path, {"new":True}, replace=True)
        self.assertEqual(len(calls), 3)
        self.assertEqual(json.loads(path.read_text()), {"new":True})

    def test_persistent_denial_is_bounded_and_preserves_status(self):
        path = self.root / "status.json"
        publish_json(path, {"old":True})
        with patch("mb_market_data.candle_supervision.os.replace", side_effect=PermissionError(errno.EACCES,"test")) as mocked:
            with self.assertRaises(PermissionError):
                publish_json(path, {"new":True}, replace=True)
        self.assertEqual(mocked.call_count, 5)
        self.assertEqual(json.loads(path.read_text()), {"old":True})

    def test_process_lock_blocks_duplicate_and_releases_on_close(self):
        path = self.root / "lock"
        code = "from pathlib import Path; from mb_market_data.candle_supervision import DirectoryLock; import sys; " \
               "lock=DirectoryLock(Path(sys.argv[1])); lock.__enter__()"
        with DirectoryLock(path):
            other = subprocess.run([sys.executable,"-c",code,str(path)], capture_output=True)
            self.assertNotEqual(other.returncode, 0)
        other = subprocess.run([sys.executable,"-c",code,str(path)], capture_output=True)
        self.assertEqual(other.returncode, 0, other.stderr)


class TestSessionRecovery(HealthFixture):
    def test_backfilled_candle_uses_later_acquisition_time_and_keeps_run_identity(self):
        write_receipt(self.run, 1, BOUNDARY+timedelta(seconds=1), close=101)
        second = self.root / "run_0002"
        capture_plan(second)
        write_receipt(second, 1, BOUNDARY+timedelta(seconds=60), close=102)
        reader = CandleSessionReader(self.root)
        reader.refresh()
        early = reader.project(BOUNDARY+timedelta(seconds=59))
        later = reader.project(BOUNDARY+timedelta(seconds=60))
        self.assertEqual(early.closed[0].candle.close, 101)
        self.assertEqual(later.closed[0].candle.close, 102)
        self.assertEqual(later.closed[0].received_at_utc, BOUNDARY+timedelta(seconds=60))
        self.assertEqual(later.closed[0].capture_run_id, "run_0002")
        self.assertEqual(later.closed[0].observed_version, 2)
        fresh = CandleSessionReader(self.root)
        fresh.refresh()
        self.assertEqual(fresh.project(BOUNDARY+timedelta(seconds=59)), early)

    def test_missing_run_and_changed_plan_fail_while_preserving_validated_versions(self):
        write_receipt(self.run, 1, BOUNDARY+timedelta(seconds=1))
        reader = CandleSessionReader(self.root)
        reader.refresh()
        self.run.rename(self.root / "removed")
        with self.assertRaises(CandleReceiptError):
            reader.refresh()
        self.assertEqual(len(reader.project(BOUNDARY+timedelta(seconds=2)).closed), 1)


class TestQuietDashboard(LiveFixture):
    def test_health_refresh_is_independent_of_paused_inspection_and_stop_is_viewer_only(self):
        state = {"state":"WARNING","severity":"warning","symbol":"TEST","message":"Synthetic candle outage"}
        controller = self.controller()
        controller.pause()
        stop = Event()
        app = create_quote_dashboard(controller, request_server_stop=stop.set, candle_health_provider=lambda:state)
        client = app.server.test_client()
        layout = client.get("/_dash-layout").data
        self.assertIn(b"Synthetic candle outage", layout)
        self.assertIn(b'"aria-live":"polite"', layout)
        before = controller.snapshot().replay_time_utc
        state.update(state="HEALTHY",severity="notice",message="Recovered")
        result = request_callback(client,app,"candle-health-tick","candle-health-tick.n_intervals",{})
        self.assertEqual(result.status_code,200)
        self.assertIn("Recovered",result.get_json()["response"]["candle-health-status"]["children"])
        self.assertEqual(controller.snapshot().replay_time_utc,before)
        self.assertFalse(stop.is_set())


class TestTrialGuards(unittest.TestCase):
    def test_corrupt_producer_status_is_a_failure_value_instead_of_a_monitor_crash(self):
        namespace=runpy.run_path(str(ROOT/"probes"/"supervise_live_candles.py"))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for malformed in ([],{"state":"RUNNING"},{"state":"RUNNING","started_at_utc":"naive"}):
                (root/"producer_status.json").write_text(json.dumps(malformed))
                self.assertEqual(namespace["producer_phase"](root,stamp(BOUNDARY))["state"],"FAILED")

    def test_live_launch_is_refused_outside_regular_session_before_authentication(self):
        namespace = runpy.run_path(str(ROOT / "probes" / "supervise_live_candles.py"))
        from argparse import Namespace
        with tempfile.TemporaryDirectory() as tmp:
            args=Namespace(symbol="SPY",synthetic_scenario=None,market_open_confirmed=True,
                           duration=720,interval=15,output_root=Path(tmp))
            with patch.dict(namespace["new_session"].__globals__, validate_window=lambda *a: (_ for _ in ()).throw(ValueError("closed"))):
                with self.assertRaises(ValueError):
                    namespace["new_session"](args)
            self.assertEqual(list(Path(tmp).iterdir()),[])

    @unittest.skipUnless(os.name == "nt", "Real Windows sharing lock")
    def test_real_windows_sharing_lock_allows_retry_after_reader_releases(self):
        import ctypes
        from ctypes import wintypes
        from threading import Timer
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                           wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"status.json"
            publish_json(path,{"old":True})
            handle=create(str(path),0x80000000,1,None,3,0x80,None)
            self.assertNotEqual(handle,ctypes.c_void_p(-1).value)
            timer=Timer(.05,lambda:close(handle))
            timer.start()
            try:
                publish_json(path,{"new":True},replace=True)
            finally:
                timer.join()
            self.assertEqual(json.loads(path.read_text()),{"new":True})


if __name__ == "__main__":
    unittest.main()
