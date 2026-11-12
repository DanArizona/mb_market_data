from __future__ import annotations

import runpy
import unittest
from datetime import date, datetime
from pathlib import Path

from mb_market_data.api_overnight_volume import ET
from mb_market_data.sampling_membership import SamplingHierarchyRevision


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROBE = PROJECT_ROOT / "probes" / "analyze_api_ov_cutoffs.py"
NAMESPACE = runpy.run_path(
    str(PROBE),
    run_name="analyze_api_ov_cutoffs_test",
)
validate_run_time = NAMESPACE["validate_run_time"]


def opening() -> SamplingHierarchyRevision:
    return SamplingHierarchyRevision(
        session_date=date(2026, 9, 24),
        revision=0,
        effective_at=datetime(2026, 9, 24, 9, 30, tzinfo=ET),
        uni_symbols=("AAA",),
        focus_symbols=(),
        hot_symbols=(),
        source="unit-test",
    )


class TestOVCutoffAnalysisProbe(unittest.TestCase):
    def test_rejects_run_before_0930_et(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot start before"):
            validate_run_time(
                opening(),
                datetime(2026, 9, 24, 9, 29, 59, tzinfo=ET),
            )

    def test_accepts_run_at_0930_et(self) -> None:
        validate_run_time(
            opening(),
            datetime(2026, 9, 24, 9, 30, tzinfo=ET),
        )

    def test_accepts_later_date(self) -> None:
        validate_run_time(
            opening(),
            datetime(2026, 9, 25, 12, 0, tzinfo=ET),
        )


if __name__ == "__main__":
    unittest.main()
