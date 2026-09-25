"""Serve a local dashboard from one completed quote journal."""

from __future__ import annotations

import argparse
import sys
import time
from functools import partial
from pathlib import Path
from threading import Event


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mb_market_data.local_dashboard_server import LocalDashboardServer
from mb_market_data.observation_overlay import (
    ObservationOverlayProjector,
    load_observation_overlay_cache,
)
from mb_market_data.quote_dashboard import create_quote_dashboard
from mb_market_data.quote_dashboard_replay import (
    QuoteDashboardReplayController,
)
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
    parser.add_argument(
        "--start-at-beginning",
        action="store_true",
        help=(
            "Open paused before the first event instead of displaying "
            "the completed final state"
        ),
    )
    parser.add_argument(
        "--observation-overlay-cache",
        type=Path,
        help=(
            "Validated immutable five-minute OHLCV cache for one symbol. "
            "When supplied, add the read-only Observation Overlay panel."
        ),
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
    stop_event = Event()

    try:
        reader = QuoteJournalReplayReader(args.database)
        timeline = reader.timeline()
        overlay_cache = (
            load_observation_overlay_cache(args.observation_overlay_cache)
            if args.observation_overlay_cache is not None
            else None
        )
        overlay_projector_factory = (
            partial(
                ObservationOverlayProjector,
                cache=overlay_cache,
                symbol=overlay_cache.symbol,
                session_date=reader.session_date,
            )
            if overlay_cache is not None
            else None
        )
        controller = QuoteDashboardReplayController(
            timeline=timeline,
            event_factory=reader.events,
            overlay_projector_factory=overlay_projector_factory,
        )
        if not args.start_at_beginning:
            controller.finish()
        replay = controller.snapshot()
        app = create_quote_dashboard(
            controller,
            request_server_stop=stop_event.set,
        )
        server = LocalDashboardServer(
            app.server,
            host=args.host,
            port=args.port,
            stop_event=stop_event,
        )
    except Exception as exc:
        print(f"Dashboard ERROR: {type(exc).__name__}: {exc}")
        return 2

    elapsed = time.perf_counter() - started
    url = f"http://{args.host}:{args.port}"
    print(f"Session date     : {reader.session_date.isoformat()}")
    if overlay_cache is not None:
        print(f"OO symbol        : {overlay_cache.symbol}")
        print(f"OO cache         : {args.observation_overlay_cache}")
    print(f"Replay events    : {timeline.event_count:,}")
    print(
        "Initial position : "
        + ("beginning (paused)" if args.start_at_beginning else "end")
    )
    print(f"Events projected : {replay.applied_event_count:,}")
    print(f"Load time        : {elapsed:.3f} seconds")
    print(f"Open in browser  : {url}")
    if not args.start_at_beginning:
        print("Click Restart to return to the beginning of the session.")
    print("Press Ctrl+C here or use Stop server in the dashboard.")
    print()

    try:
        server.serve_until_stopped()
    except Exception as exc:
        print(f"Dashboard server ERROR: {type(exc).__name__}: {exc}")
        return 2
    else:
        print("Dashboard stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
