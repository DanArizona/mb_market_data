from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from mb_market_data.observation_overlay import ObservationOverlayProjector
from mb_market_data.overlay_halts import (
    OverlayHaltInterval,
    load_daily_nasdaq_halt_events,
    load_overlay_halt_intervals,
)
from test_observation_overlay import cache


class TestOverlayHalts(unittest.TestCase):
    def test_daily_nasdaq_episodes_use_first_observation_not_release(self) -> None:
        session = date(2026, 11, 13)
        records = {
            "ONFO@2026-11-13T14:31:09.153000+00:00": {
                "first_observed_at": "2026-11-13T14:32:03.368991+00:00",
                "last_observed_at": "2026-11-13T20:59:03+00:00",
                "record": {
                    "symbol": "ONFO", "halt_date": "11/13/2026",
                    "halt_time": "09:31:09.153", "reason_code": "LUDP",
                    "retrieval_mode": "CURRENT",
                    "resumption_date": "11/13/2026",
                    "resumption_quote_time": "09:31:09",
                    "resumption_trade_time": "09:41:09",
                },
            },
            "OTHER@2026-11-13T14:31:09+00:00": {"record": {"symbol": "OTHER"}},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.json"
            path.write_text(json.dumps(records), encoding="utf-8")
            intervals = load_daily_nasdaq_halt_events(
                path, symbol="ONFO", session_date=session
            )
        self.assertEqual(len(intervals), 1)
        event = intervals[0]
        self.assertEqual(event.start_et.strftime("%H:%M:%S.%f"), "09:31:09.153000")
        self.assertIsNone(event.end_et)  # Final release is NOT first-observed knowledge.
        self.assertEqual(event.reason_code, "LUDP")
        self.assertEqual(event.first_observed_at_utc.isoformat(),
                         "2026-11-13T14:32:03.368991+00:00")

    def test_daily_halt_rejects_mismatched_key_and_event_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.json"
            path.write_text(json.dumps({
                "ONFO@2026-11-13T14:30:00+00:00": {
                    "first_observed_at": "2026-11-13T14:32:03+00:00",
                    "last_observed_at": "2026-11-13T14:32:03+00:00",
                    "record": {
                        "symbol": "ONFO", "halt_date": "11/13/2026",
                        "halt_time": "09:31:09.153", "reason_code": "LUDP",
                        "retrieval_mode": "CURRENT",
                    },
                },
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "key differs"):
                load_daily_nasdaq_halt_events(
                    path, symbol="ONFO", session_date=date(2026, 11, 13)
                )

    def test_daily_halt_not_revealed_before_first_observation(self) -> None:
        from mb_market_data.observation_overlay import ET
        session = date(2026, 9, 24)
        event = OverlayHaltInterval(
            datetime(2026, 9, 24, 9, 31, 9, tzinfo=ET),
            None,
            "LUDP",
            datetime(2026, 9, 24, 13, 32, 3, tzinfo=timezone.utc),
        )
        projector = ObservationOverlayProjector(
            cache=cache(), symbol="TEST", session_date=session,
            halt_intervals=(event,),
        )
        before = datetime(2026, 9, 24, 13, 32, 2, tzinfo=timezone.utc)
        observed = datetime(2026, 9, 24, 13, 32, 3, tzinfo=timezone.utc)
        self.assertEqual(projector.snapshot(before).halt_intervals, ())
        self.assertEqual(projector.snapshot(observed).halt_intervals, (event,))

    def test_quote_only_projector_has_no_fabricated_candles(self) -> None:
        session = date(2026, 9, 24)
        projector = ObservationOverlayProjector(
            cache=None, symbol="TEST", session_date=session,
        )
        state = projector.snapshot(
            datetime(2026, 9, 24, 13, 35, tzinfo=timezone.utc)
        )
        self.assertIsNone(state.cache)
        self.assertEqual(state.candles, ())

    def test_confirmed_knrx_halt_does_not_mark_prior_gap(self) -> None:
        session = date(2026, 9, 28)
        record = {
            "symbol": "KNRX", "halt_date": "09/28/2026",
            "halt_time": "09:36:48", "reason_code": "M",
            "resumption_date": "09/28/2026",
            "resumption_trade_time": "09:41:48",
            "retrieval_mode": "HISTORICAL",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "normalized.json"
            path.write_text(json.dumps([record, {**record, "symbol": "OTHER"}]))
            intervals = load_overlay_halt_intervals(
                path, symbol="KNRX", session_date=session
            )
        self.assertEqual(
            [
                (
                    interval.start_et.strftime("%H:%M:%S"),
                    interval.end_et.strftime("%H:%M:%S"),
                )
                for interval in intervals
            ],
            [("09:36:48", "09:41:48")],
        )
        self.assertFalse(
            any(i.start_et.hour == 9 and i.start_et.minute < 36 for i in intervals)
        )

    def test_projector_exposes_markers_only_after_event_time(self) -> None:
        # Reuse the cache fixture's session and symbol.
        from mb_market_data.observation_overlay import ET
        session = date(2026, 9, 24)
        interval = OverlayHaltInterval(
            datetime(2026, 9, 24, 9, 36, 48, tzinfo=ET),
            datetime(2026, 9, 24, 9, 41, 48, tzinfo=ET),
            "M",
        )
        projector = ObservationOverlayProjector(
            cache=cache(),
            symbol="TEST",
            session_date=session,
            halt_intervals=(interval,),
        )
        before = datetime(2026, 9, 24, 13, 36, 47, tzinfo=timezone.utc)
        at = datetime(2026, 9, 24, 13, 36, 48, tzinfo=timezone.utc)
        self.assertEqual(projector.snapshot(before).halt_intervals, ())
        self.assertEqual(projector.snapshot(at).halt_intervals, (interval,))


if __name__ == "__main__":
    unittest.main()
