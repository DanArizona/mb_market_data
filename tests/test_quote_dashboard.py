from __future__ import annotations

import unittest

from mb_market_data.quote_dashboard import create_quote_dashboard
from mb_market_data.quote_dashboard_view import build_quote_dashboard_view
from mb_market_data.quote_event_state import QuoteEventStateProjector


class TestQuoteDashboard(unittest.TestCase):
    def test_serves_layout_and_packaged_assets(self) -> None:
        view = build_quote_dashboard_view(
            QuoteEventStateProjector().snapshot()
        )
        app = create_quote_dashboard(view)
        client = app.server.test_client()

        page = client.get("/")
        layout = client.get("/_dash-layout")
        stylesheet = client.get("/assets/quote_dashboard.css")
        favicon = client.get("/assets/favicon.svg")
        for response in (page, layout, stylesheet, favicon):
            self.addCleanup(response.close)

        self.assertEqual(page.status_code, 200)
        self.assertEqual(layout.status_code, 200)
        self.assertIn(b"market-state-grid", layout.data)
        self.assertEqual(stylesheet.status_code, 200)
        self.assertEqual(favicon.status_code, 200)


if __name__ == "__main__":
    unittest.main()
