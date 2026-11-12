from __future__ import annotations

import io
import json
import os
import runpy
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import acquisition_health as health
    import polling_alarm as alarm
    import polling_monitor_supervisor as supervisor
except ModuleNotFoundError:
    from mb_market_data import acquisition_health as health, polling_alarm as alarm, polling_monitor_supervisor as supervisor
MonitorIncidentHistory = supervisor.MonitorIncidentHistory


def denied():
    error = PermissionError("SYNTHETIC Windows status file replacement denied")
    error.winerror = 5
    return error


class StatusRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_temporary_windows_denial_retries_without_removing_old_status(self):
        path = self.root / "status.json"
        path.write_text('{"old": true}')
        replace = os.replace
        attempts = []
        def blocked_then_open(source, target):
            attempts.append(source.read_bytes())
            if len(attempts) < 3:
                self.assertEqual(json.loads(path.read_text()), {"old": True})
                raise denied()
            replace(source, target)
        with patch.object(health.os, "replace", side_effect=blocked_then_open), patch.object(health.time, "sleep"):
            health.atomic_json(path, {"new": True})
        self.assertEqual(json.loads(path.read_text()), {"new": True})
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(set(attempts)), 1)
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_persistent_denial_has_bounded_attempts_and_preserves_status(self):
        path = self.root / "status.json"
        path.write_text("OLD")
        with patch.object(health.os, "replace", side_effect=denied()) as replace, patch.object(health.time, "sleep"):
            with self.assertRaises(PermissionError):
                health.atomic_json(path, {"new": True})
        self.assertEqual(replace.call_count, 8)
        self.assertEqual(path.read_text(), "OLD")
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_other_io_errors_are_not_retried(self):
        with patch.object(health.os, "replace", side_effect=OSError("disk error")) as replace:
            with self.assertRaises(OSError): health.atomic_json(self.root / "status.json", {})
        self.assertEqual(replace.call_count, 1)

    def test_incident_recovery_and_acknowledgement_survive_new_process(self):
        path = self.root / "monitor_incidents.jsonl"
        history = MonitorIncidentHistory(path)
        history.update({"monitor:STOPPED": "Inspector exited"}, "2026-10-09T19:00:00+00:00")
        self.assertIsNone(history.flush())
        restarted = MonitorIncidentHistory(path)
        restarted.update({}, "2026-10-09T19:00:05+00:00", can_resolve=False)
        self.assertIn("monitor:STOPPED", restarted.urgent())
        restarted.update({}, "2026-10-09T19:00:10+00:00")
        restarted.flush()
        recovered = MonitorIncidentHistory(path)
        self.assertFalse(recovered.urgent())
        self.assertEqual(len(recovered.notices()), 1)
        recovered.acknowledge("2026-10-09T19:00:11+00:00")
        recovered.flush()
        self.assertFalse(MonitorIncidentHistory(path).notices())

    def test_failed_history_write_keeps_pending_incident_and_alarm(self):
        history = MonitorIncidentHistory(self.root / "incidents.jsonl")
        history.update({"monitor:status_write": "denied"}, "2026-10-09T19:00:00+00:00")
        with patch.object(Path, "open", side_effect=denied()):
            self.assertIn("could not be saved", history.flush())
        self.assertTrue(history.pending)
        self.assertIn("monitor:status_write", history.urgent())
        self.assertIsNone(history.flush())
        self.assertFalse(history.pending)

    def test_history_corruption_is_an_error_not_empty_history(self):
        path = self.root / "incidents.jsonl"
        path.write_text("{bad JSON}\n")
        self.assertIsNotNone(MonitorIncidentHistory(path).read_error)

    def cli(self):
        adjacent = Path(health.__file__).parent / "polling_health.py"
        source = adjacent if adjacent.exists() else Path(__file__).resolve().parents[1] / "probes/polling_health.py"
        return runpy.run_path(str(source), run_name="synthetic_status_cli")

    def test_inspector_keeps_checking_when_status_publication_fails(self):
        cli = self.cli()
        function = cli["monitor"]
        start = datetime(2026, 10, 9, 9, 30, tzinfo=health.ET)
        config = health.HealthConfig(self.root, self.root / "2026-10-09.sqlite3", date(2026, 10, 9), start, start + timedelta(hours=6.5))
        snapshot = {"session_date": str(config.session_date), "journal": str(config.journal), "channels": {},
                    "observed_at_utc": start.isoformat(), "journal_read": "OK", "journal_error": None,
                    "journal_read_seconds": 0, "grace_seconds": 90, "process_inspection": {"known": True}}
        checks = []
        def inspect(_):
            checks.append(1)
            return dict(snapshot)
        with patch.dict(function.__globals__, {"wait_for_stop": lambda *a, **kw: None if len(checks) < 2 else "Q key"}), \
             patch.object(health, "inspect_health", side_effect=inspect), \
             patch.object(health, "persist_health", side_effect=[denied(), []]), redirect_stdout(io.StringIO()):
            self.assertEqual(function(config, popups=False), 0)
        self.assertEqual(len(checks), 2)
        directory = self.root / "output/polling_health/2026-10-09"
        published = json.loads((directory / "latest.json").read_text())
        self.assertIn("status_output_error", published)
        self.assertFalse(config.journal.exists())

    def test_watchdog_keeps_inspector_and_alarm_alive_during_status_denial(self):
        cli = self.cli()
        supervise = cli["supervise_monitor"]
        namespace = supervise.__globals__
        start = datetime(2026, 10, 9, 9, 30, tzinfo=health.ET)
        config = health.HealthConfig(self.root, self.root / "2026-10-09.sqlite3", date(2026, 10, 9), start, start + timedelta(hours=6.5))
        directory = self.root / "output/polling_health/2026-10-09"
        directory.mkdir(parents=True)
        (directory / "latest.json").write_text(json.dumps({"monitor_pid": 123, "monitor_token": "token", "journal": str(config.journal),
            "observed_at_utc": start.isoformat(), "channels": {}, "journal_read": "OK", "process_inspection": {"known": True}}))
        child = SimpleNamespace(pid=123, poll=lambda: None)
        payloads = []
        desktop = SimpleNamespace(start=lambda: None, close=lambda: None, publish=lambda p: payloads.append(p))
        original = health.atomic_json
        attempts = 0
        def publish(path, value):
            nonlocal attempts
            if path.name == "supervisor.json":
                attempts += 1
                if attempts == 1: raise denied()
            original(path, value)
        with patch.dict(namespace, {"os": SimpleNamespace(name="nt", getpid=os.getpid), "wait_for_stop": lambda *a: None if attempts < 2 else "Q key",
                "uuid4": lambda: SimpleNamespace(hex="token")}), \
             patch.object(health, "atomic_json", side_effect=publish), \
             patch.object(alarm, "DesktopAlarm", return_value=desktop), \
             patch("subprocess.Popen", return_value=child), \
             patch.object(supervisor, "stop_monitor_child") as stop_child, redirect_stdout(io.StringIO()):
            self.assertEqual(supervise(config), 0)
            self.assertEqual(attempts, 2)
            stop_child.assert_called_once()
        self.assertIn("monitor:status_write", payloads[0]["urgent"])
        self.assertFalse(payloads[1]["urgent"])
        self.assertTrue(payloads[1]["notices"])
        self.assertEqual(json.loads((directory / "supervisor_stop.json").read_text())["stopped_by"], "Q key")
        self.assertTrue(MonitorIncidentHistory(directory / "monitor_incidents.jsonl").notices())
        self.assertFalse(config.journal.exists())

    def test_inspector_status_failure_is_urgent_after_close(self):
        snapshot = {"channels": {}, "observed_at_utc": "2026-10-09T22:00:00+00:00", "journal_read": "OK", "process_inspection": {"known": True},
                    "status_output_error": "cannot publish"}
        payload = alarm.alarm_payload(snapshot, health.alert_conditions(snapshot), [], window_active=False, status_path=self.root / "STATUS.txt", poller_windows="test")
        self.assertIn("monitor:status_output", payload["urgent"])

    @unittest.skipUnless(os.name == "nt", "native Windows sharing semantics")
    def test_real_windows_reader_lock_releases_and_same_publication_succeeds(self):
        import ctypes
        import threading
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        path = self.root / "native_reader_lock.json"
        path.write_text('{"old": true}')
        handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        timer = threading.Timer(0.15, lambda: kernel.CloseHandle(handle))
        timer.start()
        try: health.atomic_json(path, {"new": True})
        finally: timer.join()
        self.assertEqual(json.loads(path.read_text()), {"new": True})


if __name__ == "__main__": unittest.main(verbosity=2)
