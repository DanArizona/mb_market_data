"""Experimental, read-only live quotes/membership Overlay; optional offline demo."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread, Timer
from unittest.mock import patch
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mb_market_data.local_dashboard_server import LocalDashboardServer
from mb_market_data.quote_dashboard import create_quote_dashboard
from mb_market_data.quote_dashboard_live import QuoteDashboardLiveController
from mb_market_data.candle_receipts import CandleReceiptReader
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
    parser.add_argument("--symbol")
    parser.add_argument("--demo", action="store_true", help="Create an isolated synthetic journal and use a simulated clock")
    parser.add_argument("--capture-demo", type=Path, help="Replay saved candle receipts against an empty synthetic quote journal")
    parser.add_argument("--candle-capture-dir", type=Path, help="Separate candle receipts for an experimental quote journal")
    parser.add_argument("--demo-speed", type=float, default=10, help="Saved candle demo clock multiplier")
    parser.add_argument("--port", type=int, default=8051)
    parser.add_argument("--batch-limit", type=int, default=32)
    parser.add_argument("--shutdown-check", choices=("ctrl-c", "q"),
                        help="Check one console stop method against synthetic data; 90-second fallback")
    args = parser.parse_args()
    if sum((args.demo, args.database is not None, args.capture_demo is not None)) != 1:
        parser.error("Choose --demo, --capture-demo, or one experimental database, exclusively")
    if args.candle_capture_dir and args.database is None:
        parser.error("--candle-capture-dir requires an experimental database")
    if args.shutdown_check and not args.demo:
        parser.error("--shutdown-check requires --demo; no saved capture or external journal is opened")
    if not 0 < args.demo_speed <= 60:
        parser.error("--demo-speed must be between 0 and 60")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.batch_limit <= 0:
        parser.error("--batch-limit must be positive")
    if args.symbol is not None and not args.symbol.strip():
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
        self.candle_dir = root / "candle_receipts"
        self.candle_dir.mkdir()
        (self.candle_dir / "request_plan.json").write_text(json.dumps({
            "symbol": self.symbol, "session_date": str(session), "frequency_minutes": 5,
            "extended_hours": False, "simulation": True,
            "request_start_et": self.boundary.astimezone(ET).replace(hour=9, minute=30).isoformat(),
            "request_end_et": self.boundary.astimezone(ET).replace(hour=16, minute=0).isoformat(),
        }), encoding="utf-8")
        self.candle_receipt(1, -10, partial=True)
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

    def candle_receipt(self, number, seconds, *, partial=False, correction=False):
        received = self.boundary + timedelta(seconds=seconds)
        candles = [{"datetime": int((self.boundary-timedelta(minutes=10)).timestamp()*1000),
                    "open":100,"high":102,"low":99,"close":101,"volume":800},
                   {"datetime": int((self.boundary-timedelta(minutes=5)).timestamp()*1000),
                    "open":101,"high":103,"low":100,"close":101.5 if partial else 101.8 if correction else 102,
                    "volume":1000 if partial else 1320 if correction else 1300}]
        body = json.dumps({"symbol":self.symbol,"simulation":True,"candles":candles}).encode()
        raw_name = f"sample_{number:04d}.response.txt"
        with (self.candle_dir / raw_name).open("xb") as out:
            out.write(body)
        receipt = {"sample_number":number,"dispatched_at_utc":(received-timedelta(seconds=.2)).isoformat(),
                   "received_at_utc":received.isoformat(),"http_status":200,"outcome":"OK",
                   "raw_response_file":raw_name,"raw_sha256":hashlib.sha256(body).hexdigest()}
        temporary = self.candle_dir / f"sample_{number:04d}.receipt.tmp"
        temporary.write_text(json.dumps(receipt), encoding="utf-8")
        temporary.rename(self.candle_dir / f"sample_{number:04d}.receipt.json")

    def run(self):
        actions = (
            (0.1, lambda: self.publish(2, 1, tuple(sorted({self.symbol, self.reference_symbol})))),
            (3, lambda: self.candle_receipt(2, 3)),
            (6, lambda: self.acquire(WatchlistKind.FOCUS, 5, 5.3, 2)),
            (8, lambda: self.acquire(WatchlistKind.UNI, 0, 2.47, 1)),
            (21, lambda: self.acquire(WatchlistKind.FOCUS, 20, 20.3, 2)),
            (25, lambda: self.candle_receipt(3, 25)),
            (31, lambda: self.acquire(WatchlistKind.UNI, 30, 30.4, 2)),
            (36, lambda: self.acquire(WatchlistKind.FOCUS, 35, 35.3, 2)),
            (50, lambda: self.candle_receipt(4, 50, correction=True)),
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
    limit = None
    expired = Event()
    shutdown_passed = True
    try:
        candle_reader = None
        if args.demo or args.capture_demo:
            output = PROJECT_ROOT / "output" / "live_overlay_offline_demo"
            output.mkdir(parents=True, exist_ok=True)
            root = Path(tempfile.mkdtemp(prefix="demo_", dir=output))
            if args.demo:
                args.symbol = args.symbol or "MSFT"
                demo = OfflineDemo(root, args.symbol, stop)
                database, now = demo.path, demo.now
                candle_reader = CandleReceiptReader(demo.candle_dir)
                source_label = "Synthetic candles, quotes and membership · simulated clock"
            else:
                candle_reader = CandleReceiptReader(args.capture_demo)
                if args.symbol and args.symbol.strip().upper() != candle_reader.symbol:
                    raise ValueError("Selected symbol differs from the candle capture")
                args.symbol = candle_reader.symbol
                while not candle_reader.caught_up:
                    candle_reader.refresh(limit=args.batch_limit)
                times = candle_reader.availability_times
                if not times:
                    raise ValueError("Saved candle demonstration needs captured receipts")
                database = root / f"{candle_reader.session_date}.sqlite3"
                QuoteObservationStore(database, session_date=candle_reader.session_date, schema_version=2).initialize()
                started = time.monotonic()
                first, last = times[0]-timedelta(seconds=2), times[-1]
                now = lambda: min(first+timedelta(seconds=(time.monotonic()-started)*args.demo_speed), last)
                source_label = f"Captured {args.symbol} candle receipts · simulated clock {args.demo_speed:g}x · quote/membership evidence not supplied"
        else:
            database, now = args.database, lambda: datetime.now(UTC)
            candle_reader = CandleReceiptReader(args.candle_capture_dir) if args.candle_capture_dir else None
            args.symbol = args.symbol or (candle_reader.symbol if candle_reader else "MSFT")
            source_label = None
        reader = QuoteJournalLiveReader(database)
        controller = QuoteDashboardLiveController(
            reader=reader, symbol=args.symbol, now=now, batch_limit=args.batch_limit, candle_reader=candle_reader,
        )
        app = create_quote_dashboard(
            controller, request_server_stop=stop.set,
            source_label=source_label,
        )
        server = LocalDashboardServer(app.server, host="127.0.0.1", port=args.port, stop_event=stop)
        print("Experimental candle receipts, quotes and membership Overlay", flush=True)
        print(f"Journal: {database}", flush=True)
        if demo:
            print("SYNTHETIC DATA · SIMULATED CLOCK · no Schwab credentials or requests", flush=True)
            demo.thread.start()
        elif args.capture_demo:
            print(source_label, flush=True)
            print("No API or password. Quote/membership journal is an empty synthetic fixture.", flush=True)
            print(f"Saved receipt period: {times[0].astimezone(ET).isoformat()} through {times[-1].astimezone(ET).isoformat()}", flush=True)
        print(f"Open http://127.0.0.1:{args.port} and select the Chart tab.", flush=True)
        print("Pause freezes the inspection time; Return to Live catches up.", flush=True)
        print("Stop server, Ctrl+C or Q in this CMD closes this viewer. Clear CMD text selection with Esc first.", flush=True)
        if args.shutdown_check:
            expected = {"ctrl-c": "Ctrl+C", "q": "Q key"}[args.shutdown_check]
            print(f"CONSOLE CHECK: press {expected} now. You do not need to open the browser.", flush=True)
            print("Automatic stop after 90 seconds. A timeout does not count as a passed keyboard check.", flush=True)
            def expire():
                expired.set()
                stop.set()
            limit = Timer(90, expire)
            limit.daemon = True
            limit.start()
        stopped_by = server.serve_until_stopped()
        if expired.is_set() and stopped_by == "stop request":
            stopped_by = "test time limit"
        if args.shutdown_check:
            shutdown_passed = stopped_by == expected and not (demo and demo.error)
            print(f"CONSOLE CHECK: {'PASS' if shutdown_passed else 'NOT CONFIRMED'}; requested {expected}, stopped by {stopped_by}.", flush=True)
        print(f"Viewer stopped by {stopped_by}; listener closed.", flush=True)
        if args.demo or args.capture_demo:
            receipt = root / "viewer_stop.json"
            receipt.write_text(json.dumps({"pid": os.getpid(), "port": server.bound_port,
                "stopped_by": stopped_by, "completed_at_utc": datetime.now(UTC).isoformat(),
                "requested_console_check": args.shutdown_check,
                "console_check_passed": shutdown_passed if args.shutdown_check else None,
                "listener_closed": True, "production_journal_access": "NONE"}, indent=2) + "\n", encoding="utf-8")
            print(f"Viewer stop receipt: {receipt}", flush=True)
    except Exception as exc:
        print(f"Live Overlay ERROR: {type(exc).__name__}: {exc}", flush=True)
        return 2
    finally:
        if limit is not None:
            limit.cancel()
            limit.join(timeout=1)
        stop.set()
        if demo and demo.thread.is_alive():
            demo.thread.join(timeout=2)
    return 2 if (demo and demo.error) or not shutdown_passed else 0


if __name__ == "__main__":
    raise SystemExit(main())
