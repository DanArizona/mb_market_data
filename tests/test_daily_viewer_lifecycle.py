"""Daily CMD status viewer lifecycle; no real controller or provider calls."""
from __future__ import annotations

import contextlib
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mb_market_data import daily_runtime


class TestDailyViewerLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.status = {
            "session_date": "2026-11-25",
            "phase": "FINISHED",
            "at": "2026-11-25T16:01:13-05:00",
            "pid": 21568,
            "selection_status": "READY; r1 PUBLISHED",
            "next_session_status": {"status": "READY"},
            "workers": {
                "uni": {"pid": 4964, "exit_code": 0, "api_state": "AVAILABLE"},
                "focus": {"pid": 8836, "exit_code": 0, "api_state": "AVAILABLE"},
                "hot": {"pid": 32780, "exit_code": 0, "api_state": "AVAILABLE"},
            },
            "hot_activated": True,
            "health": {"channels": {
                "uni": {"state": "WINDOW_ENDED"},
                "focus": {"state": "WINDOW_ENDED"},
                "hot": {"state": "WINDOW_ENDED", "missing_slot_count": 12},
            }},
            "final_status": True,
        }
        self._save()

    def _save(self) -> None:
        (self.root / "status.json").write_text(
            json.dumps(self.status), encoding="utf-8",
        )

    def test_attended_start_viewer_returns_at_final_receipt(self) -> None:
        out = io.StringIO()
        with (
            patch.object(daily_runtime.os, "system") as cls,
            patch.object(daily_runtime.time, "sleep") as delay,
            contextlib.redirect_stdout(out),
        ):
            result = daily_runtime.watch(
                self.root, exit_when_final=True,
                quit_key=lambda: False,
            )
        self.assertEqual(result, 0)
        self.assertIn("Daily session final report verified; returning to CMD", out.getvalue())
        delay.assert_not_called()
        self.assertEqual(
            json.loads((self.root / "status.json").read_text())["final_status"], True
        )

    def test_finished_phase_without_cleanup_receipt_does_not_autoquit(self) -> None:
        self.status["final_status"] = False
        self._save()
        out = io.StringIO()
        with (
            patch.object(daily_runtime.os, "system"),
            contextlib.redirect_stdout(out),
        ):
            result = daily_runtime.watch(
                self.root, exit_when_final=True, quit_key=lambda: True
            )
        self.assertEqual(result, 0)
        self.assertNotIn("final report verified", out.getvalue())
        self.assertIn("closed with Q", out.getvalue())

    def test_explicit_watch_q_exits_only_viewer_not_data(self) -> None:
        self.status["phase"] = "COLLECTING"
        self.status["final_status"] = False
        self._save()
        before = (self.root / "status.json").read_bytes()
        out = io.StringIO()
        with (
            patch.object(daily_runtime.os, "system"),
            patch.object(daily_runtime.time, "sleep") as delay,
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(
                daily_runtime.watch(
                    self.root, exit_when_final=False, quit_key=lambda: True
                ),
                0,
            )
        delay.assert_not_called()
        self.assertEqual((self.root / "status.json").read_bytes(), before)
        self.assertIn("collection/controller unchanged", out.getvalue())

    def test_attended_start_routes_to_autoexit_but_explicit_watch_does_not(self) -> None:
        proc = SimpleNamespace(pid=21568, poll=lambda: None)
        out = io.StringIO()
        with (
            patch.object(daily_runtime, "owner_available", return_value=True),
            patch.object(daily_runtime, "show_authorization", return_value=None),
            patch.object(daily_runtime, "watch", return_value=0) as watcher,
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(
                daily_runtime.wait_for_controller(
                    proc, self.root, self.root / "test.log",
                ),
                0,
            )
        watcher.assert_called_once_with(self.root, exit_when_final=True)


if __name__ == "__main__":
    unittest.main()
