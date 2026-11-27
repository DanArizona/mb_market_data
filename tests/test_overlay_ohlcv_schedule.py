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
            max_requests_per_minute=overrides.get("limit", 5),
        )

    def invoke_tick(self, worker, client, h, m, s, symbol="TEST", **kw):
        f = times(h, m, s)
        return worker.tick(
            client=client, selected_symbol=symbol,
            now=et(h, m, s), now_factory=lambda: next(f), **kw,
        )

    def write_ledger(self, attempts):
        self.worker.state_root.mkdir(parents=True, exist_ok=True)
        self.worker.ledger_path.write_text(json.dumps({
            "contract": "overlay-owned-ohlcv-schedule-v1",
            "session_date": DAY.isoformat(),
            "attempts": attempts,
        }), encoding="utf-8")

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

    def test_five_attempts_per_rolling_minute_include_failures_and_expire(self):
        client = fixture()
        for second, symbol in zip(range(55, 60), ("TEST", "AAPL", "MSFT", "NVDA", "INTC")):
            outcome = self.invoke_tick(self.worker, client, 9, 35, second, symbol=symbol)
            # The fixture rejects mismatched symbols after the call, so these
            # failed attempts must consume exactly the same budget as success.
            self.assertEqual(outcome.status, "PUBLISHED" if symbol == "TEST" else "FAILED")
        self.assertEqual(len(client.requests), 5)
        for second in (0, 54):
            with self.subTest(next_minute_second=second):
                sixth = self.invoke_tick(self.worker, client, 9, 36, second, symbol="ZYN")
                self.assertEqual(sixth.status, "BUDGET_EXHAUSTED")
                self.assertEqual(len(client.requests), 5)
        # The first reservation expires at exactly sixty seconds. A different
        # symbol can proceed during the SAME completed five-minute candle slot.
        sixth = self.invoke_tick(self.worker, client, 9, 36, 55, symbol="ZYN")
        self.assertEqual(sixth.status, "FAILED")
        self.assertEqual(sixth.slot_close_et, et(9, 35))
        self.assertEqual(len(client.requests), 6)
        duplicate = self.invoke_tick(self.worker, client, 9, 36, 56)
        self.assertEqual(duplicate.status, "ALREADY_ATTEMPTED")
        self.assertEqual(len(client.requests), 6)

    def test_rolling_budget_survives_restart_and_candle_boundary(self):
        client = fixture()
        for second, symbol in zip(range(55, 60), ("TEST", "AAPL", "MSFT", "NVDA", "INTC")):
            self.invoke_tick(self.worker, client, 9, 39, second, symbol=symbol)
        restarted = self.build()
        blocked = self.invoke_tick(restarted, client, 9, 40, 5)
        self.assertEqual(blocked.slot_close_et, et(9, 40))
        self.assertEqual(blocked.status, "BUDGET_EXHAUSTED")
        self.assertEqual(len(client.requests), 5)
        allowed = self.invoke_tick(restarted, client, 9, 40, 55)
        self.assertEqual(allowed.status, "PUBLISHED")
        self.assertEqual(allowed.candle_count, 2)
        self.assertEqual(len(client.requests), 6)

    def test_legacy_ledger_reservations_keep_budget_and_duplicate_protection(self):
        original_rows = [{
            "slot_close_et": et(9, 35).isoformat(),
            "symbol": symbol,
            "state": state,
            "reserved_at_utc": et(9, 35, second).astimezone(UTC).isoformat(),
        } for symbol, state, second in (("TEST", "PUBLISHED", 5), ("AAPL", "RESERVED", 8))]
        self.write_ledger(original_rows)
        restarted = self.build()
        client = fixture()
        self.assertEqual(self.invoke_tick(restarted, client, 9, 35, 10).status, "ALREADY_ATTEMPTED")
        self.assertEqual(client.requests, [])
        for second, symbol in ((12, "MSFT"), (14, "NVDA"), (16, "INTC")):
            self.assertEqual(self.invoke_tick(restarted, client, 9, 35, second, symbol=symbol).status, "FAILED")
        self.assertEqual(self.invoke_tick(restarted, client, 9, 35, 18, symbol="ZYN").status, "BUDGET_EXHAUSTED")
        self.assertEqual(len(client.requests), 3)
        saved = json.loads(self.worker.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["contract"], "overlay-owned-ohlcv-schedule-v1")
        self.assertEqual(saved["attempts"][:2], original_rows)

    def test_invalid_reservation_times_block_before_provider_call(self):
        valid_row = {
            "slot_close_et": et(9, 35).isoformat(),
            "symbol": "OLD", "state": "RESERVED",
        }
        malformed_rows = [valid_row, None] + [dict(valid_row, reserved_at_utc=value)
            for value in (None, 123, "broken", "2026-11-25T14:35:05")]
        client = fixture()
        for row in malformed_rows:
            with self.subTest(row=row):
                self.write_ledger([row])
                with self.assertRaisesRegex(ValueError, "reservation timestamp"):
                    self.invoke_tick(self.worker, client, 9, 35, 10)
                self.assertEqual(client.requests, [])

    def test_future_reservation_stays_charged_after_clock_rollback(self):
        self.write_ledger([{
            "slot_close_et": et(9, 35).isoformat(),
            "symbol": "OLD", "state": "RESERVED",
            "reserved_at_utc": et(9, 36, 5).astimezone(UTC).isoformat(),
        }])
        limited = self.build(limit=1)
        client = fixture()
        for stamp in ((9, 35, 55), (9, 36, 5), (9, 37, 4)):
            with self.subTest(stamp=stamp):
                self.assertEqual(self.invoke_tick(limited, client, *stamp).status, "BUDGET_EXHAUSTED")
        self.assertEqual(client.requests, [])
        self.assertEqual(self.invoke_tick(limited, client, 9, 37, 5).status, "PUBLISHED")
        self.assertEqual(len(client.requests), 1)

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
        for limit in (0, -1, True, False, 1.5, "5", None):
            with self.subTest(limit=limit):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    self.build(limit=limit)
        self.assertEqual(self.build(limit=6).max_requests_per_minute, 6)
        with self.assertRaisesRegex(ValueError, "five-minute boundary"):
            self.build(opens_at=et(9, 31))
        self.assertFalse((self.root / "owned-state").exists())


if __name__ == "__main__":
    unittest.main()
