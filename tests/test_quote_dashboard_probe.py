from __future__ import annotations

import runpy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROBE = PROJECT_ROOT / "probes" / "dashboard_quote_observation_journal.py"
NAMESPACE = runpy.run_path(
    str(PROBE),
    run_name="dashboard_quote_observation_journal_test",
)
parse_args = NAMESPACE["parse_args"]


class TestQuoteDashboardProbe(unittest.TestCase):
    def test_accepts_optional_observation_overlay_cache(self) -> None:
        with patch.object(
            sys,
            "argv",
            [
                str(PROBE),
                "journal.sqlite3",
                "--observation-overlay-cache",
                "TEST-2026-09-24.json",
                "--start-at-beginning",
            ],
        ):
            args = parse_args()

        self.assertEqual(args.database, Path("journal.sqlite3"))
        self.assertEqual(
            args.observation_overlay_cache,
            Path("TEST-2026-09-24.json"),
        )
        self.assertTrue(args.start_at_beginning)


if __name__ == "__main__":
    unittest.main()
