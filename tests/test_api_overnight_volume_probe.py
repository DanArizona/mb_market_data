from __future__ import annotations

import runpy
import unittest
from datetime import date, datetime
from pathlib import Path

from mb_market_data.api_overnight_volume import ET
from mb_market_data.sampling_membership import SamplingHierarchyRevision


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROBE = PROJECT_ROOT / "probes" / "acquire_api_overnight_volume.py"
NAMESPACE = runpy.run_path(
    str(PROBE),
    run_name="acquire_api_overnight_volume_test",
)
validate_run_time = NAMESPACE["validate_run_time"]


def opening() -> SamplingHierarchyRevision:
    return SamplingHierarchyRevision(
        session_date=date(2026, 9, 21),
        revision=0,
        effective_at=datetime(2026, 9, 21, 9, 30, tzinfo=ET),
        uni_symbols=("AAA",),
        focus_symbols=(),
        hot_symbols=(),
        source="unit-test",
    )


class TestAPIOvernightVolumeProbe(unittest.TestCase):
    def test_accepts_run_at_0900_et(self) -> None:
        validate_run_time(
            opening(),
            datetime(2026, 9, 21, 9, 0, tzinfo=ET),
        )

    def test_rejects_run_before_decision_window_closes(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot start before"):
            validate_run_time(
                opening(),
                datetime(2026, 9, 21, 8, 59, 59, tzinfo=ET),
            )

    def test_rejects_run_at_opening_effective_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "must start before"):
            validate_run_time(
                opening(),
                datetime(2026, 9, 21, 9, 30, tzinfo=ET),
            )


if __name__ == "__main__":
    unittest.main()
