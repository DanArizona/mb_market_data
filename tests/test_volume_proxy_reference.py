"""Offline check against the user's existing accepted Focus selector, not a mock."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import unittest
from unittest.mock import patch

# Import is mandatory: a missing reference implementation is an error, not a skip.
from schwab_watchlists.api_ov_focus_production import API_OV_FOCUS_SOURCE
from mb_market_data.volume_proxy_experiment import run_comparison
from test_volume_proxy_experiment import Client, VolumeProxyTests


class InstalledReferenceTests(unittest.TestCase):
    def exercise(self, missing=()):
        fixture = VolumeProxyTests('test_555_symbols_two_quote_calls_then_555_history_calls_same_client')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.opening_for(555)
        client = Client(fixture.clock, missing=missing, root=fixture.output)
        with patch('mb_market_data.schwab_quotes.datetime', fixture.clock.datetime_type()), redirect_stdout(StringIO()):
            report = run_comparison(
                client, opening_path=fixture.opening, output_dir=fixture.output,
                snapshot_at=fixture.clock(), clock=fixture.clock,
                sleep=fixture.clock.sleep, request_interval=0)
        self.assertEqual(report['quote_requests'], 2)
        self.assertEqual(report['history_requests'], 555)
        self.assertIsNotNone(report['reference_artifacts'], report)
        proposal = Path(report['reference_artifacts']['proposal'])
        revision = json.loads(proposal.read_text())
        self.assertEqual(revision['revision'], 1)
        self.assertEqual(revision['source'], API_OV_FOCUS_SOURCE)
        self.assertEqual(len(revision['focus_symbols']), 40)
        self.assertEqual(len(revision['uni_symbols']), 555)
        self.assertEqual(client.closed, 0)
        return report

    def test_accepted_selector_builds_r1_from_saved_candle_evidence(self):
        report = self.exercise()
        self.assertEqual(report['result'], 'COMPARISON_COMPLETE')
        self.assertEqual(report['common_count'], 40)

    def test_missing_proxy_does_not_prevent_accepted_reference_r1(self):
        report = self.exercise(missing=['X000'])
        self.assertEqual(report['result'], 'COMPARISON_WITH_MISSING_PROXY_DATA')
        self.assertIn('X000', report['Focus_r1'])
        self.assertEqual(report['common_count'], 39)
