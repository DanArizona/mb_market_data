"""Offline one-shot OHLCV preflight gates; no Schwab, files, or token access."""
from datetime import datetime, timedelta
from pathlib import Path
import sys
import unittest
from mb_market_data.observation_overlay import ET

PROBES = str(Path(__file__).resolve().parents[1] / "probes")
if PROBES not in sys.path:
    sys.path.insert(0, PROBES)
from acquire_live_overlay_ohlcv_once import verify_collecting_channels


class LivePreflightTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 25, 10, 58, 30, tzinfo=ET)
        def stamp(age):
            return (self.now - timedelta(seconds=age)).isoformat()
        self.state = {
            "phase": "COLLECTING_DEGRADED", "hot_activated": True,
            "health": {
                "channels": {name: {
                    "state": "RUNNING", "committed_acquisitions": 100,
                    "last_committed_slot_utc": stamp(30),
                    "latest_outcomes": {"quote": 5},
                    "missing_slot_count": 2 if name == "hot" else 0,
                } for name in ("uni", "focus", "hot")},
                "journal_read": "OK",
            },
            "workers": {name: {
                "exit_code": None, "api_state": "AVAILABLE", "api_at": stamp(2),
            } for name in ("uni", "focus", "hot")},
        }

    def check(self):
        verify_collecting_channels(self.state, now=self.now)

    def test_old_hot_gaps_accepted_when_all_workers_active(self):
        self.check()

    def test_stopped_phase_rejected(self):
        self.state["phase"] = "COLLECTION_STOPPED"
        with self.assertRaisesRegex(ValueError, "not collecting"):
            self.check()

    def test_unavailable_api_or_exited_worker_rejected(self):
        hot = self.state["workers"]["hot"]
        hot["api_state"] = "RATE_LIMIT_BACKOFF"
        with self.assertRaisesRegex(ValueError, "API unavailable"):
            self.check()
        hot["api_state"] = "AVAILABLE"
        hot["exit_code"] = 1
        with self.assertRaisesRegex(ValueError, "exited"):
            self.check()

    def test_stale_acquisition_and_request_errors_rejected(self):
        hot = self.state["health"]["channels"]["hot"]
        hot["last_committed_slot_utc"] = (
            self.now - timedelta(minutes=5)
        ).isoformat()
        with self.assertRaisesRegex(ValueError, "Stale"):
            self.check()
        hot["last_committed_slot_utc"] = self.now.isoformat()
        hot["latest_outcomes"]["request_error"] = 1
        with self.assertRaisesRegex(ValueError, "recent request error"):
            self.check()
