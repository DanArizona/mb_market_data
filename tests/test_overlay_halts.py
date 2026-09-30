from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from mb_market_data.observation_overlay import ObservationOverlayProjector
from mb_market_data.overlay_halts import (
    OverlayHaltInterval,
    load_overlay_halt_intervals,
)
from tests.test_observation_overlay import cache


class TestOverlayHalts(unittest.TestCase):
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
