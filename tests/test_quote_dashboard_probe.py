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
    def test_accepts_daily_nasdaq_events_with_cache(self) -> None:
        with patch.object(sys, "argv", [
            str(PROBE), "journal.sqlite3", "--observation-overlay-cache",
            "ONFO-candles.json", "--nasdaq-daily-events-json", "events.json",
        ]):
            args = parse_args()
        self.assertEqual(args.nasdaq_daily_events_json, Path("events.json"))
        self.assertIsNone(args.nasdaq_halts_json)

    def test_daily_nasdaq_events_accept_quote_only_symbol(self) -> None:
        with patch.object(sys, "argv", [
            str(PROBE), "journal.sqlite3", "--overlay-symbol", "ONFO",
            "--nasdaq-daily-events-json", "events.json",
            "--start-at-beginning",
        ]):
            args = parse_args()
        self.assertEqual(args.overlay_symbol, "ONFO")
        self.assertIsNone(args.observation_overlay_cache)
        self.assertEqual(args.nasdaq_daily_events_json, Path("events.json"))

    def test_daily_nasdaq_events_require_symbol_or_cache(self) -> None:
        with patch.object(sys, "argv", [
            str(PROBE), "journal.sqlite3", "--nasdaq-daily-events-json",
            "events.json",
        ]):
            with self.assertRaises(SystemExit) as raised:
                parse_args()
        self.assertEqual(raised.exception.code, 2)

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
