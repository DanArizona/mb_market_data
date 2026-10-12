"""One-symbol candle-only live trial, using an empty isolated quote fixture."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
from threading import Event

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from mb_market_data.candle_session import CandleSessionReader
from mb_market_data.candle_supervision import health_view, publish_json, stamp
from mb_market_data.local_dashboard_server import LocalDashboardServer
from mb_market_data.quote_dashboard import create_quote_dashboard
from mb_market_data.quote_dashboard_live import QuoteDashboardLiveController
from mb_market_data.quote_journal_live import QuoteJournalLiveReader
from mb_market_data.quote_observation_store import QuoteObservationStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--port", type=int, default=8052)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    stop = Event()
    try:
        candle_reader = CandleSessionReader(args.session)
        output = PROJECT_ROOT / "output" / "candle_trial_viewers"
        output.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix="viewer_", dir=output))
        database = folder / f"{candle_reader.session_date}.sqlite3"
        QuoteObservationStore(database, session_date=candle_reader.session_date, schema_version=2).initialize()
        plan = json.loads((candle_reader.directory / "session_plan.json").read_text(encoding="utf-8"))
        controller = QuoteDashboardLiveController(reader=QuoteJournalLiveReader(database),
            symbol=candle_reader.symbol, candle_reader=candle_reader,
            now=lambda: datetime.now(timezone.utc))
        label = ("Synthetic candle drill" if plan["simulation"] else "Live SPY candle trial")
        app = create_quote_dashboard(controller, request_server_stop=stop.set,
            source_label=label + " · actual acquisition clock · quote/membership evidence not supplied",
            candle_health_provider=lambda: health_view(candle_reader.directory))
        server = LocalDashboardServer(app.server, host="127.0.0.1", port=args.port, stop_event=stop)
        print(f"Open http://127.0.0.1:{args.port} and select Chart", flush=True)
        print("Stop server closes this viewer. The candle producer and supervision continue independently.", flush=True)
        server.serve_until_stopped()
        publish_json(folder / "viewer_stop_receipt.json", {"stopped_at_utc": stamp(),
                     "session": str(candle_reader.directory), "producer_stop_requested": False,
                     "production_journal_access": "NONE"})
        return 0
    except Exception as exc:
        print(f"Candle viewer stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
