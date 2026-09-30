from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from mb_market_data.observation_overlay import ObservationOverlayProjector
from mb_market_data.overlay_halts import load_overlay_halt_markers
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
            markers = load_overlay_halt_markers(path, symbol="KNRX", session_date=session)
        self.assertEqual([(m.kind, m.time_et.strftime("%H:%M:%S")) for m in markers],
                         [("halt", "09:36:48"), ("resume", "09:41:48")])
        self.assertFalse(any(m.time_et.hour == 9 and m.time_et.minute < 36 for m in markers))

    def test_projector_exposes_markers_only_after_event_time(self) -> None:
        # Reuse the cache fixture's session and symbol.
        from mb_market_data.overlay_halts import OverlayHaltMarker
        from mb_market_data.observation_overlay import ET
        session = date(2026, 9, 24)
        marker = OverlayHaltMarker(datetime(2026, 9, 24, 9, 36, 48, tzinfo=ET), "halt", "M")
        projector = ObservationOverlayProjector(
            cache=cache(), symbol="TEST", session_date=session, halt_markers=(marker,)
        )
        before = datetime(2026, 9, 24, 13, 36, 47, tzinfo=timezone.utc)
        at = datetime(2026, 9, 24, 13, 36, 48, tzinfo=timezone.utc)
        self.assertEqual(projector.snapshot(before).halt_markers, ())
        self.assertEqual(projector.snapshot(at).halt_markers, (marker,))


if __name__ == "__main__":
    unittest.main()
