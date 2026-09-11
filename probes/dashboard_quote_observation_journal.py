"""Serve a local dashboard from one completed quote journal."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mb_market_data.quote_dashboard import create_quote_dashboard
from mb_market_data.quote_dashboard_view import build_quote_dashboard_view
from mb_market_data.quote_event_state import QuoteEventStateProjector
from mb_market_data.quote_journal_replay import QuoteJournalReplayReader


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Project a completed daily quote journal and serve its current "
            "state as a local Dash dashboard."
        )
    )
    parser.add_argument("database", type=Path, help="Daily .sqlite3 file")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Local interface to serve (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8050,
        help="Local port to serve (default: 8050)",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    return args


def main() -> int:
    args = parse_args()
    print()
    print("MasterBot market-state dashboard")
    print("=" * 79)
    print(f"Journal          : {args.database}")
    print("Loading replay state...")
    started = time.perf_counter()

    try:
        reader = QuoteJournalReplayReader(args.database)
        projector = QuoteEventStateProjector(
            session_date=reader.session_date
        )
        for event in reader.events():
            projector.apply(event)
        view = build_quote_dashboard_view(projector.snapshot())
        app = create_quote_dashboard(view)
    except Exception as exc:
        print(f"Dashboard ERROR: {type(exc).__name__}: {exc}")
        return 2

    elapsed = time.perf_counter() - started
    url = f"http://{args.host}:{args.port}"
    print(f"Session date     : {reader.session_date.isoformat()}")
    print(f"Events projected : {view.event_count:,}")
    print(f"Current symbols  : {view.unique_member_count:,}")
    print(f"Load time        : {elapsed:.3f} seconds")
    print(f"Open in browser  : {url}")
    print("Press Ctrl+C here to stop the dashboard.")
    print()

    try:
        app.run(host=args.host, port=args.port, debug=False)
    except KeyboardInterrupt:
        print("Dashboard stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
