from __future__ import annotations

import runpy
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROBE = PROJECT_ROOT / "probes" / "acquire_observation_overlay_ohlcv.py"
NAMESPACE = runpy.run_path(
    str(PROBE),
    run_name="acquire_observation_overlay_ohlcv_test",
)
parse_args = NAMESPACE["parse_args"]
parse_session_date = NAMESPACE["parse_session_date"]


class TestObservationOverlayAcquisitionProbe(unittest.TestCase):
    def test_accepts_explicit_completed_session_request(self) -> None:
        with patch.object(
            sys,
            "argv",
            [
                str(PROBE),
                "--symbol",
                "ATCH",
                "--session-date",
                "2026-09-24",
                "--output-dir",
                "evidence",
            ],
        ):
            args = parse_args()

        self.assertEqual(args.symbol, "ATCH")
        self.assertEqual(args.session_date, "2026-09-24")
        self.assertEqual(args.output_dir, "evidence")
        self.assertEqual(parse_session_date(args.session_date), date(2026, 9, 24))

    def test_rejects_malformed_session_date(self) -> None:
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            parse_session_date("09/24/2026")


if __name__ == "__main__":
    unittest.main()
