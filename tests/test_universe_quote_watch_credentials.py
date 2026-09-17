from __future__ import annotations

import runpy
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from mb_market_data.watchlist_polling import PollWindow
from mb_tools.schwab_secure import DEFAULT_POLLING_REFRESH_MARGIN


ET = ZoneInfo("America/New_York")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROBE = PROJECT_ROOT / "probes" / "probe_universe_quote_watch.py"
PROBE_NAMESPACE = runpy.run_path(
    str(PROBE),
    run_name="probe_universe_quote_watch_credentials",
)
polling_credential_required_through = PROBE_NAMESPACE[
    "polling_credential_required_through"
]
prepare_polling_client = PROBE_NAMESPACE["prepare_polling_client"]
main = PROBE_NAMESPACE["main"]


class TestUniverseQuoteWatchCredentials(unittest.TestCase):
    def test_exact_slot_run_requires_end_plus_refresh_margin(self) -> None:
        started = datetime(2026, 9, 16, 8, 0, tzinfo=ET)
        window = PollWindow(
            start_at=datetime(2026, 9, 16, 9, 30, tzinfo=ET),
            end_at=datetime(2026, 9, 16, 16, 0, tzinfo=ET),
        )

        required = polling_credential_required_through(
            started_at=started,
            poll_window=window,
            stop_at=None,
            max_samples=None,
            interval_seconds=60,
        )

        self.assertEqual(
            required,
            window.end_at + DEFAULT_POLLING_REFRESH_MARGIN,
        )

    def test_fixed_interval_run_accounts_for_last_sample_start(self) -> None:
        started = datetime(2026, 9, 16, 12, 0, tzinfo=ET)

        required = polling_credential_required_through(
            started_at=started,
            poll_window=None,
            stop_at=None,
            max_samples=3,
            interval_seconds=20,
        )

        self.assertEqual(
            required,
            started + timedelta(seconds=40)
            + DEFAULT_POLLING_REFRESH_MARGIN,
        )

    def test_past_planned_end_still_requires_margin_from_startup(self) -> None:
        started = datetime(2026, 9, 16, 18, 0, tzinfo=ET)
        window = PollWindow(
            start_at=datetime(2026, 9, 16, 9, 30, tzinfo=ET),
            end_at=datetime(2026, 9, 16, 16, 0, tzinfo=ET),
        )

        required = polling_credential_required_through(
            started_at=started,
            poll_window=window,
            stop_at=None,
            max_samples=None,
            interval_seconds=60,
        )

        self.assertEqual(
            required,
            started + DEFAULT_POLLING_REFRESH_MARGIN,
        )

    def test_unexpected_interactive_auth_is_rejected_and_closed(self) -> None:
        status = Mock()
        client = Mock()

        def request_auth(_config, **kwargs):
            kwargs["call_on_auth"]("https://example.invalid/auth")
            return client

        with patch.dict(
            prepare_polling_client.__globals__,
            {
                "load_secure_schwab_config": Mock(return_value=object()),
                "read_schwab_token_status": Mock(return_value=status),
                "make_client_from_config": request_auth,
            },
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "No browser was opened",
            ):
                prepare_polling_client(
                    Path("secure.ecfg"),
                    "password",
                    timeout=20,
                    required_through=datetime(
                        2026, 9, 16, 17, 15, tzinfo=ET
                    ),
                )

        client.close.assert_called_once_with()

    def test_failed_preflight_creates_no_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_root = Path(temporary_directory) / "output"
            arguments = SimpleNamespace(
                interval=60.0,
                max_samples=None,
                watchlist_revision=0,
                timeout=20,
                journal_root="journal",
                journal_schema_version=1,
                symbols=["SPY"],
                stop_at=None,
                ecfg="secure.ecfg",
                watchlist_kind="focus",
                output_root=str(output_root),
                fields="all",
                batch_size=400,
                universe_csv="unused.csv",
            )
            with (
                patch.dict(
                    main.__globals__,
                    {
                        "parse_args": Mock(return_value=arguments),
                        "load_symbols": Mock(return_value=("SPY",)),
                        "resolve_ecfg": Mock(
                            return_value=Path("secure.ecfg")
                        ),
                        "prepare_polling_client": Mock(
                            side_effect=RuntimeError("expired")
                        ),
                    },
                ),
                patch("getpass.getpass", return_value="password"),
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()),
            ):
                result = main()

            self.assertEqual(result, 2)
            self.assertFalse(output_root.exists())


if __name__ == "__main__":
    unittest.main()
