"""Optional Plotly checks; no Dash, credentials, or external network needed."""
from datetime import timedelta
import os
from pathlib import Path
import unittest
from test_daily_membership import Fixture
import test_daily_live_connection as connection
from mb_market_data.daily_live_overlay import overlay_snapshot, PAGE
from mb_market_data.observation_overlay_view import build_observation_overlay_figure

class RenderTests(Fixture):
    def test_quote_only_chart_has_no_fabricated_candles(self):
        data,_=overlay_snapshot(self.state,'INTC',at=self.at)
        fig=build_observation_overlay_figure(data)
        self.assertFalse(any(t.type=='candlestick' for t in fig.data))
        self.assertIn(".join('\\n')", PAGE)
        self.assertNotIn(".join('\n')", PAGE)
    def test_same_existing_figure_renders_dynamic_live_records(self):
        f=connection.LiveConnectionTests();f.setUp();self.addCleanup(f.doCleanups);f.proof()
        data,_=overlay_snapshot(f.state,'INTC',at=f.open+timedelta(seconds=61))
        figure=build_observation_overlay_figure(data)
        self.assertTrue(figure.data)
        self.assertEqual(data.current_band.value,'hot')
        self.assertEqual([v.band.value for v in data.membership_transitions],['hot','focus','hot'])
        output=os.environ.get('MB_OVERLAY_PREVIEW')
        if output:
            figure.update_layout(title='SYNTHETIC OFFLINE PROOF — INTC: Hot → Focus → Hot; no live prices')
            figure.write_html(output,include_plotlyjs=True,auto_open=False)

if __name__=='__main__':unittest.main(verbosity=2)
