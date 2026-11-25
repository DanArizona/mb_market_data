"""Isolated owned OHLCV cadence: no network, credentials or running collector."""
from __future__ import annotations

from datetime import datetime, date, timedelta, timezone
import json
import tempfile
from pathlib import Path
import unittest

from mb_market_data.observation_overlay import ET
from mb_market_data.overlay_ohlcv_schedule import (
    OwnedOHLCVSchedule, completed_slot,
)
from test_overlay_live_ohlcv import StubClient, raw_candle

UTC = timezone.utc
DAY = date(2026, 11, 25)


def et(hour, minute, second=0):
    return datetime(2026, 11, 25, hour, minute, second, tzinfo=ET)


def times(hour, minute, second=0):
    return iter((
        et(hour, minute, second).astimezone(UTC),
        (et(hour, minute, second) + timedelta(seconds=1)).astimezone(UTC),
    ))


def fixture():
    return StubClient({
        "symbol": "TEST",
        "candles": [
            raw_candle(9, 30, 123),
            raw_candle(9, 35, 456),
        ],
    })


class OwnedScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.opens = et(9, 30)
        self.closes = et(16, 0)
        self.worker = self.build()

    def build(self, **overrides):
        return OwnedOHLCVSchedule(
            session_date=DAY,
            opens_at=overrides.get("opens_at", self.opens),
            closes_at=overrides.get("closes_at", self.closes),
            state_root=self.root / "owned-state",
            evidence_root=self.root / "isolated-evidence",
            max_symbols_per_slot=overrides.get("limit", 2),
        )

    def invoke_tick(self, worker, client, h, m, s, symbol="TEST", **kw):
        f = times(h, m, s)
        return worker.tick(
            client=client, selected_symbol=symbol,
            now=et(h, m, s), now_factory=lambda: next(f), **kw,
        )

    def test_no_provider_call_before_completion_plus_five_seconds(self):
        client = fixture()
        for stamp in ((9, 30, 0), (9, 34, 59), (9, 35, 0), (9, 35, 4)):
            outcome = self.invoke_tick(self.worker, client, *stamp)
            self.assertEqual(outcome.status, "NOT_DUE")
        self.assertEqual(client.requests, [])
        self.assertFalse((self.root / "owned-state").exists())

    def test_publishes_one_request_at_first_boundary_and_no_duplicate(self):
        client = fixture()
        published = self.invoke_tick(self.worker, client, 9, 35, 5)
        self.assertEqual(published.status, "PUBLISHED")
        self.assertEqual(published.slot_close_et, et(9, 35))
        self.assertEqual(published.candle_count, 1)
        self.assertTrue(published.bundle_path.is_dir())
        self.assertEqual(len(client.requests), 1)
        same = self.invoke_tick(self.worker, client, 9, 39, 0)
        self.assertEqual(same.status, "ALREADY_ATTEMPTED")
        restarted = self.build()
        duplicate = self.invoke_tick(restarted, client, 9, 39, 30)
        self.assertEqual(duplicate.status, "ALREADY_ATTEMPTED")
        self.assertEqual(len(client.requests), 1)
        ledger = json.loads((self.root / "owned-state" / "ledger.json").read_text())
        self.assertEqual(ledger["attempts"][0]["state"], "PUBLISHED")
        self.assertEqual(ledger["attempts"][0]["symbol"], "TEST")

    def test_new_slot_includes_next_completed_bar_and_never_provisional(self):
        client = fixture()
        first = self.invoke_tick(self.worker, client, 9, 35, 5)
        second = self.invoke_tick(self.worker, client, 9, 40, 5)
        self.assertEqual(second.status, "PUBLISHED")
        self.assertEqual(second.candle_count, 2)
        self.assertEqual(len(client.requests), 2)
        self.assertNotEqual(first.bundle_path, second.bundle_path)

    def test_owner_absent_or_selection_missing_cannot_request(self):
        client = fixture()
        self.assertEqual(
            self.invoke_tick(self.worker, client, 9, 35, 6, owner_active=lambda: False).status,
            "OWNER_UNAVAILABLE",
        )
        self.assertEqual(
            self.invoke_tick(self.worker, client, 9, 35, 6, symbol=None).status,
            "NO_SELECTION",
        )
        self.assertEqual(client.requests, [])
        self.assertFalse((self.root / "owned-state").exists())

    def test_two_symbol_budget_per_slot_is_bounded(self):
        client = fixture()
        self.assertEqual(self.invoke_tick(self.worker, client, 9, 35, 5).status, "PUBLISHED")
        # A second symbol can be attempted in the same slot, but the source
        # intentionally rejects its mismatched symbol instead of publishing.
        second = self.invoke_tick(self.worker, client, 9, 35, 8, symbol="AAPL")
        self.assertEqual(second.status, "FAILED")
        self.assertEqual(second.error_type, "ObservationOverlayDataError")
        self.assertEqual(len(client.requests), 2)
        third = self.invoke_tick(self.worker, client, 9, 35, 10, symbol="MSFT")
        self.assertEqual(third.status, "BUDGET_EXHAUSTED")
        self.assertEqual(len(client.requests), 2)

    def test_failure_is_recorded_without_repeated_provider_call(self):
        client = StubClient({"symbol": "TEST", "candles": []})
        first = self.invoke_tick(self.worker, client, 9, 35, 5)
        self.assertEqual(first.status, "NOT_READY")
        self.assertEqual(first.error_type, "LiveOHLCVNotReady")
        self.assertEqual(
            self.invoke_tick(self.worker, client, 9, 35, 10).status,
            "ALREADY_ATTEMPTED",
        )
        self.assertEqual(len(client.requests), 1)
        ledger = json.loads((self.root / "owned-state" / "ledger.json").read_text())
        self.assertEqual(ledger["attempts"][0]["state"], "NOT_READY")
        self.assertNotIn("password", ledger["attempts"][0])
        self.assertNotIn("token", ledger["attempts"][0])

    def test_early_close_bounds_are_used_not_hardcoded_regular_close(self):
        close = et(13, 0)
        self.assertEqual(
            completed_slot(et(13, 0, 5), opens_at=self.opens, closes_at=close),
            close,
        )
        self.assertEqual(
            completed_slot(et(13, 1, 0), opens_at=self.opens, closes_at=close),
            close,
        )
        self.assertIsNone(
            completed_slot(et(13, 2, 1), opens_at=self.opens, closes_at=close)
        )
        early = self.build(closes_at=close)
        client = fixture()
        final = self.invoke_tick(early, client, 13, 1, 0)
        self.assertEqual(final.status, "PUBLISHED")
        self.assertEqual(final.slot_close_et, close)
        self.assertEqual(client.requests[0][1]["endDate"], close)
        self.assertEqual(
            self.invoke_tick(early, client, 13, 1, 30).status, "ALREADY_ATTEMPTED",
        )
        self.assertEqual(
            self.invoke_tick(early, client, 13, 2, 1).status, "NOT_DUE",
        )
        self.assertEqual(len(client.requests), 1)

    def test_rejects_broken_schedule_before_writing(self):
        with self.assertRaisesRegex(ValueError, "date differs"):
            self.build(closes_at=datetime(2026, 11, 26, 13, tzinfo=ET))
        with self.assertRaisesRegex(ValueError, "one or two symbols"):
            self.build(limit=3)
        with self.assertRaisesRegex(ValueError, "five-minute boundary"):
            self.build(opens_at=et(9, 31))
        self.assertFalse((self.root / "owned-state").exists())


if __name__ == "__main__":
    unittest.main()
