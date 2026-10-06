"""Experimental, read-only live quotes/membership Overlay; optional offline demo."""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mb_market_data.local_dashboard_server import LocalDashboardServer
from mb_market_data.quote_dashboard import create_quote_dashboard
from mb_market_data.quote_dashboard_live import QuoteDashboardLiveController
from mb_market_data.quote_journal_live import QuoteJournalLiveReader
from mb_market_data.quote_observation_store import PollRunProvenance, QuoteObservationStore
from mb_market_data.sampling_membership import SamplingHierarchyRevision
from mb_market_data.schwab_quotes import QuoteBatchResult, QuoteResult, QuoteStatus
from mb_market_data.watchlist_polling import PollRequest, PollSlot, WatchlistKind

ET = ZoneInfo("America/New_York")
UTC = timezone.utc


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", nargs="?", type=Path)
    parser.add_argument("--symbol", default="MSFT")
    parser.add_argument("--demo", action="store_true", help="Create an isolated synthetic journal and use a simulated clock")
    parser.add_argument("--port", type=int, default=8051)
    parser.add_argument("--batch-limit", type=int, default=32)
    args = parser.parse_args()
    if args.demo == (args.database is not None):
        parser.error("Choose --demo or one experimental database, exclusively")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.batch_limit <= 0:
        parser.error("--batch-limit must be positive")
    if not args.symbol.strip():
        parser.error("--symbol must be nonblank")
    if args.database and any(
        "quote_observation_journal_v2_api_opening" in part.lower()
        for part in args.database.resolve().parts
    ):
        parser.error("This prototype is for isolated experimental journals; production opening journals are excluded")
    return args


class OfflineDemo:
    """Only this opt-in simulator writes, to its newly created synthetic DB."""

    def __init__(self, root: Path, symbol: str, stop: Event):
        self.root = root
        self.symbol = symbol.upper()
        self.reference_symbol = "SPY" if self.symbol != "SPY" else "QQQ"
        self.stop = stop
        self.started = time.monotonic()
        session = datetime.now(ET).date()
        self.boundary = datetime.combine(session, datetime.min.time(), tzinfo=ET).replace(hour=13, minute=30).astimezone(UTC)
        self.path = root / f"{session}.sqlite3"
        self.store = QuoteObservationStore(self.path, session_date=session, schema_version=2)
        self.store.initialize()
        self.store.record_run(PollRunProvenance(
            run_id="synthetic-live-overlay-demo",
            started_at=self.boundary - timedelta(minutes=3),
            software_version="experimental-offline-demo",
            configuration={"simulation": True, "market_data_source": "synthetic"},
        ))
        self.publish(0, -120, ())
        self.publish(1, -120, (self.reference_symbol,))
        self.thread = Thread(target=self.run, name="synthetic-overlay-writer", daemon=True)
        self.error: str | None = None

    def now(self):
        return self.boundary + timedelta(seconds=-5 + 2 * (time.monotonic() - self.started))

    def publish(self, revision, effective_seconds, focus):
        # Explicitly synthetic publication times, matching the simulated clock.
        published = self.boundary + timedelta(seconds=effective_seconds - 0.9)
        with patch("mb_market_data.quote_observation_store._utc_now", return_value=published):
            self.store.record_membership_revision(SamplingHierarchyRevision(
                session_date=self.boundary.astimezone(ET).date(), revision=revision,
                effective_at=self.boundary + timedelta(seconds=effective_seconds),
                uni_symbols=tuple(sorted({self.symbol, self.reference_symbol})),
                focus_symbols=focus, hot_symbols=(), source="synthetic-live-overlay-demo",
                reason="synthetic membership example",
                metadata={"simulation": True, "production": False},
            ))

    def acquire(self, kind, scheduled, completed, revision):
        symbols = tuple(sorted({self.symbol, self.reference_symbol}))
        start = self.boundary + timedelta(seconds=scheduled)
        end = self.boundary + timedelta(seconds=completed)
        request = PollRequest(PollSlot(kind, start), revision, symbols, start)
        result = QuoteBatchResult(
            results=tuple(QuoteResult(
                symbol=symbol, status=QuoteStatus.QUOTE,
                quote={"symbol": symbol, "quote": {"lastPrice": 100 + completed / 10, "totalVolume": 1000 + int(completed * 10)}},
                detail="synthetic offline demo", batch_number=1,
                request_started_at_utc=start, response_received_at_utc=end,
            ) for symbol in symbols), request_count=1, batch_size=400, unexpected_symbols=(),
        )
        self.store.record_acquisition("synthetic-live-overlay-demo", request, result, completed_at=end)

    def run(self):
        actions = (
            (0.1, lambda: self.publish(2, 1, tuple(sorted({self.symbol, self.reference_symbol})))),
            (6, lambda: self.acquire(WatchlistKind.FOCUS, 5, 5.3, 2)),
            (8, lambda: self.acquire(WatchlistKind.UNI, 0, 2.47, 1)),
            (21, lambda: self.acquire(WatchlistKind.FOCUS, 20, 20.3, 2)),
            (31, lambda: self.acquire(WatchlistKind.UNI, 30, 30.4, 2)),
            (36, lambda: self.acquire(WatchlistKind.FOCUS, 35, 35.3, 2)),
        )
        try:
            for seconds, action in actions:
                deadline = self.boundary + timedelta(seconds=seconds)
                while self.now() < deadline:
                    if self.stop.wait(0.05):
                        return
                if self.stop.is_set():
                    return
                action()
                print(f"Synthetic demo committed at simulated {self.now().astimezone(ET):%H:%M:%S} ET", flush=True)
            print("Synthetic demo writes complete. Inspect, seek, or Return to Live.", flush=True)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            print(f"Synthetic demo ERROR: {self.error}", flush=True)
            self.stop.set()


def main():
    args = parse_args()
    stop = Event()
    demo = None
    try:
        if args.demo:
            output = PROJECT_ROOT / "output" / "live_overlay_offline_demo"
            output.mkdir(parents=True, exist_ok=True)
            root = Path(tempfile.mkdtemp(prefix="demo_", dir=output))
            demo = OfflineDemo(root, args.symbol, stop)
            database, now = demo.path, demo.now
        else:
            database, now = args.database, lambda: datetime.now(UTC)
        reader = QuoteJournalLiveReader(database)
        controller = QuoteDashboardLiveController(
            reader=reader, symbol=args.symbol, now=now, batch_limit=args.batch_limit,
        )
        app = create_quote_dashboard(
            controller, request_server_stop=stop.set,
            source_label="Synthetic data · simulated clock" if demo else None,
        )
        server = LocalDashboardServer(app.server, host="127.0.0.1", port=args.port, stop_event=stop)
        print("Experimental live quotes and membership Overlay", flush=True)
        print(f"Journal: {database}", flush=True)
        if demo:
            print("SYNTHETIC DATA · SIMULATED CLOCK · no Schwab credentials or requests", flush=True)
            demo.thread.start()
        print(f"Open http://127.0.0.1:{args.port} and select the Chart tab.", flush=True)
        print("Pause freezes the inspection time; Return to Live catches up.", flush=True)
        print("Stop server or Ctrl+C closes this viewer.", flush=True)
        server.serve_until_stopped()
    except Exception as exc:
        print(f"Live Overlay ERROR: {type(exc).__name__}: {exc}", flush=True)
        return 2
    finally:
        stop.set()
        if demo and demo.thread.is_alive():
            demo.thread.join(timeout=2)
    return 2 if demo and demo.error else 0


if __name__ == "__main__":
    raise SystemExit(main())
