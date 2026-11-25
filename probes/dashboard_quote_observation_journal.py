"""Serve a local dashboard from one completed quote journal."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from threading import Event

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mb_market_data.local_dashboard_server import LocalDashboardServer
from mb_market_data.polling_health_status import read_health_status
from mb_market_data.overlay_halts import (
    load_daily_nasdaq_halt_events,
    load_overlay_halt_intervals,
)
from mb_market_data.observation_overlay import (
    ET,
    ObservationOverlayProjector,
    load_observation_overlay_cache,
)
from mb_market_data.quote_dashboard import create_quote_dashboard
from mb_market_data.overlay_indexed_selection import IndexedOverlaySymbolSelection
from mb_market_data.overlay_live_tail import ReadOnlyJournalTail
from mb_market_data.overlay_live_ohlcv import LiveOHLCVReader
from mb_market_data.overlay_session_catalog import inspect_session
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
        "--real-time-data-root", type=Path,
        help=(
            "Explicit daily-controller data root for read-only, committed "
            "Real Time mode; requires matching current ET session journal."
        ),
    )
    parser.add_argument(
        "--real-time-ohlcv-root", type=Path,
        help=(
            "OPTIONAL read-only independent immutable live OHLCV evidence folder. "
            "Requires --real-time-data-root and --overlay-symbol. "
            "This viewer NEVER acquires candles, authenticates, or starts a client."
        ),
    )
    parser.add_argument(
        "--enable-ohlcv-selection",
        action="store_true",
        help=(
            "EXPERIMENTAL explicit opt-in, loopback only: write nonsecret "
            "selected-symbol capture intent to an OHLCV-enabled daily controller. "
            "Requires --real-time-data-root and --real-time-ohlcv-root. "
            "No provider calls from the viewer."
        ),
    )
    parser.add_argument(
        "--enable-manual-membership-actions",
        action="store_true",
        help=(
            "ATTENDED, EXPLICIT OPT-IN ONLY: expose Preview and Confirm for "
            "the existing daily membership worker. A confirmed action can "
            "change production membership. Never used by ordinary Real Time."
        ),
    )
    parser.add_argument("--polling-health-directory", type=Path,
                        help="Read-only monitor status folder; defaults to the journal's output/polling_health/SESSION_DATE")
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
    parser.add_argument(
        "--overlay-symbol",
        help=(
            "Enable a selected-symbol, read-only quote Overlay without a "
            "candle cache; adding a validated OHLCV cache later enables candles"
        ),
    )
    halts = parser.add_mutually_exclusive_group()
    halts.add_argument(
        "--nasdaq-halts-json",
        type=Path,
        help="Historical Nasdaq probe normalized.json for Overlay halt intervals",
    )
    halts.add_argument(
        "--nasdaq-daily-events-json",
        type=Path,
        help=(
            "Completed daily_session/DATE/nasdaq/events.json for causally "
            "observed Nasdaq halt markers (never infer release times)"
        ),
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    if args.real_time_data_root is not None and args.start_at_beginning:
        parser.error("--start-at-beginning is not supported for Real Time attachment")
    if args.real_time_ohlcv_root is not None and args.real_time_data_root is None:
        parser.error("Live OHLCV evidence requires an explicit Real Time attachment")
    if args.real_time_ohlcv_root is not None and not args.overlay_symbol:
        parser.error("Live OHLCV evidence requires a selected --overlay-symbol")
    if args.real_time_ohlcv_root is not None and args.observation_overlay_cache:
        parser.error("Live OHLCV root and fixed historical OHLCV cache cannot be combined")
    if args.enable_ohlcv_selection and (
        args.real_time_data_root is None or args.real_time_ohlcv_root is None
    ):
        parser.error("OHLCV selected-symbol publishing requires Real Time and its evidence root")
    if args.enable_ohlcv_selection and args.host != "127.0.0.1":
        parser.error("OHLCV selected-symbol publishing requires exact loopback")
    if args.enable_manual_membership_actions and args.real_time_data_root is None:
        parser.error("Manual actions require an explicit Real Time controller attachment")
    if args.enable_manual_membership_actions and args.host != "127.0.0.1":
        parser.error("Manual actions require exact loopback host 127.0.0.1")
    if args.nasdaq_halts_json and not args.observation_overlay_cache:
        parser.error("--nasdaq-halts-json requires --observation-overlay-cache")
    if args.nasdaq_daily_events_json and not (
        args.observation_overlay_cache or args.overlay_symbol
    ):
        parser.error("--nasdaq-daily-events-json requires --overlay-symbol or --observation-overlay-cache")
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

    def request_viewer_stop() -> None:
        """Browser Stop server targets only this local Overlay listener."""
        print(
            "VIEWER STOP REQUESTED: shutting down local Overlay server only; "
            "daily collection is independent.",
            flush=True,
        )
        stop_event.set()

    try:
        reader = QuoteJournalReplayReader(args.database)
        timeline = reader.timeline()
        overlay_cache = (
            load_observation_overlay_cache(args.observation_overlay_cache)
            if args.observation_overlay_cache is not None
            else None
        )
        overlay_symbol = (
            overlay_cache.symbol if overlay_cache is not None
            else args.overlay_symbol.strip().upper() if args.overlay_symbol else None
        )
        if args.overlay_symbol and not args.overlay_symbol.strip():
            raise ValueError("--overlay-symbol must be nonblank")
        if overlay_cache is not None and args.overlay_symbol and (
            args.overlay_symbol.strip().upper() != overlay_cache.symbol
        ):
            raise ValueError("--overlay-symbol differs from OHLCV cache symbol")
        live_ohlcv_reader = (
            LiveOHLCVReader(
                args.real_time_ohlcv_root, session_date=reader.session_date
            )
            if args.real_time_ohlcv_root is not None else None
        )
        poll_seconds_by_run = reader.poll_seconds_by_run() if overlay_symbol else {}

        def make_overlay_projector(selected_symbol: str) -> ObservationOverlayProjector:
            """Use only the exact selected-symbol/date cache and halt evidence."""
            selected = selected_symbol.strip().upper()
            fixed_cache = (
                overlay_cache
                if overlay_cache is not None and overlay_cache.symbol == selected
                else None
            )
            live_cache = (
                live_ohlcv_reader.latest(selected)
                if live_ohlcv_reader is not None else None
            )
            return ObservationOverlayProjector(
                cache=live_cache if live_cache is not None else fixed_cache,
                symbol=selected,
                session_date=reader.session_date,
                poll_seconds_by_run=poll_seconds_by_run,
                halt_intervals=(
                    load_overlay_halt_intervals(
                        args.nasdaq_halts_json,
                        symbol=selected,
                        session_date=reader.session_date,
                    )
                    if args.nasdaq_halts_json is not None
                    else load_daily_nasdaq_halt_events(
                        args.nasdaq_daily_events_json,
                        symbol=selected,
                        session_date=reader.session_date,
                    )
                    if args.nasdaq_daily_events_json is not None
                    else ()
                ),
            )

        overlay_projector_factory = (
            partial(make_overlay_projector, overlay_symbol)
            if overlay_symbol is not None else None
        )
        selected_index = (
            IndexedOverlaySymbolSelection(reader, make_overlay_projector)
            if overlay_symbol is not None else None
        )
        controller = QuoteDashboardReplayController(
            timeline=timeline,
            event_factory=reader.events,
            overlay_projector_factory=overlay_projector_factory,
            symbol_projector_factory=make_overlay_projector if overlay_symbol else None,
            indexed_symbol_projector_factory=selected_index,
            overlay_end_at_utc=(
                overlay_cache.request_end_et if overlay_cache is not None else None
            ),
        )
        live_tail = None
        operator_actions = None
        ohlcv_selection_bridge = None
        last_ohlcv_selected = {"symbol": None}
        def request_ohlcv_symbol(symbol: str) -> None:
            if ohlcv_selection_bridge is None:
                raise RuntimeError("OHLCV symbol capture is not enabled")
            ohlcv_selection_bridge.publish(symbol)
            last_ohlcv_selected["symbol"] = symbol
        if args.real_time_data_root is not None:
            today_et = datetime.now(ET).date()
            if reader.session_date != today_et:
                raise ValueError(
                    "Real Time attachment requires today's ET journal: "
                    f"{reader.session_date} is not {today_et}"
                )
            matched = inspect_session(
                args.real_time_data_root, reader.session_date
            )
            if Path(matched.journal_path).resolve() != args.database.resolve():
                raise ValueError("Real Time controller plan/journal identity mismatch")
            if args.enable_ohlcv_selection:
                # Separate from manual Membership writer. Only a controller
                # started with the experimental OHLCV option has this plan.
                from mb_market_data.overlay_ohlcv_worker import OwnedOHLCVSelection
                plan_path = (
                    args.real_time_data_root / "output" / "daily_session"
                    / str(reader.session_date) / "plan.json"
                )
                owned_plan = json.loads(plan_path.read_text(encoding="utf-8"))
                if owned_plan.get("overlay_ohlcv_enabled") is not True:
                    raise ValueError("Current daily controller was not launched with OHLCV")
                if Path(owned_plan["overlay_ohlcv_evidence_root"]).resolve() != (
                    args.real_time_ohlcv_root.resolve()
                ):
                    raise ValueError("OHLCV evidence folder differs from the controller plan")
                owner_receipt = json.loads(
                    (plan_path.parent / "owner.json").read_text(encoding="utf-8")
                )
                ohlcv_selection_bridge = OwnedOHLCVSelection(
                    owned_plan, owner_pid=owner_receipt["pid"],
                )
            controller.seek(datetime.now(timezone.utc))
            live_tail = ReadOnlyJournalTail(
                data_root=args.real_time_data_root,
                reader=reader, controller=controller,
                indexed_selection=selected_index,
            )
            if args.enable_manual_membership_actions:
                # Only this opt-in path imports the existing operator backend.
                # The read-only and Historical viewers never construct it.
                from mb_market_data.overlay_manual_membership import (
                    RealTimeMembershipOperator,
                )
                operator_actions = RealTimeMembershipOperator(
                    args.real_time_data_root, reader.session_date
                )
        elif not args.start_at_beginning:
            controller.finish()
        live_refresh = live_tail.refresh if live_tail is not None else None
        if live_tail is not None and live_ohlcv_reader is not None:
            # The viewer reads only completed, immutable raw-backed bundles.
            # A separate producer owns any authenticated client / SDK calls.
            def refresh_with_live_candles() -> tuple[int, str]:
                newly_committed, detail = live_tail.refresh()
                current = controller.snapshot().observation_overlay
                if current is None:
                    return newly_committed, detail + " · OHLCV: no symbol selected"
                if (
                    ohlcv_selection_bridge is not None
                    and last_ohlcv_selected["symbol"] != current.symbol
                ):
                    try:
                        # First refresh also arms the initial chart symbol.
                        # A future or stopped session cannot publish intent.
                        request_ohlcv_symbol(current.symbol)
                    except (OSError, ValueError, RuntimeError):
                        detail += " · OHLCV selection: no active authorized owner"
                cache = live_ohlcv_reader.latest(current.symbol)
                if cache is None:
                    return (
                        newly_committed,
                        detail + " · OHLCV: no independent completed-candle snapshot",
                    )
                changed = controller.install_live_ohlcv_cache(cache)
                label = (
                    f" · OHLCV: {len(cache.candles)} completed "
                    f"Schwab-format source-backed candles for {cache.symbol}, last received "
                    f"{cache.acquired_at_utc.isoformat()} UTC "
                    "(immutable read-only evidence, no client in viewer)"
                )
                return newly_committed + int(changed), detail + label
            live_refresh = refresh_with_live_candles
        replay = controller.snapshot()
        app = create_quote_dashboard(
            controller,
            request_server_stop=request_viewer_stop,
            live_mode=live_tail is not None,
            live_refresh=live_refresh,
            live_symbol_selection_sink=(
                request_ohlcv_symbol if ohlcv_selection_bridge is not None
                else None
            ),
            operator_actions=operator_actions,
            acquisition_health_reader=partial(read_health_status,
                args.polling_health_directory or args.database.parent.parent / "polling_health" / str(reader.session_date),
                journal=args.database, session_date=reader.session_date),
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
    if overlay_symbol is not None:
        print(f"OO symbol        : {overlay_symbol}")
    if overlay_cache is not None:
        print(f"OO cache         : {args.observation_overlay_cache}")
    elif overlay_symbol is not None:
        print("OO candles       : none (quote-only historical replay)")
    if args.nasdaq_halts_json is not None:
        print(f"Nasdaq halts     : {args.nasdaq_halts_json}")
    if args.nasdaq_daily_events_json is not None:
        print(f"Daily Nasdaq     : {args.nasdaq_daily_events_json}")
    print(f"Replay events    : {timeline.event_count:,}")
    print(
        "Initial position : "
        + (
            "live committed prefix (read-only)" if live_tail is not None
            else "beginning (paused)" if args.start_at_beginning else "end"
        )
    )
    print(f"Events projected : {replay.applied_event_count:,}")
    print(f"Load time        : {elapsed:.3f} seconds")
    print(f"Open in browser  : {url}")
    if live_tail is not None:
        print("Live tail         : committed additions every 5 seconds; no Schwab requests")
        print(
            "Operator controls : "
            + (
                "EXPLICITLY ENABLED · Preview/Confirm may queue production changes"
                if operator_actions is not None
                else "DISABLED · read-only"
            )
        )
        print("Controller phase  : saved evidence only; process liveness is NOT verified")
        if args.real_time_ohlcv_root is not None:
            print(f"Live OHLCV evidence: {args.real_time_ohlcv_root}")
            print("OHLCV source: independent immutable files only; no Schwab client in viewer")
            print(
                "OHLCV capture selection: "
                + ("EXPLICITLY ENABLED (nonsecret request only)"
                   if ohlcv_selection_bridge is not None else "DISABLED (read-only)")
            )
        print("Real Time read-only: Historical requires a separate viewer launch.")
    elif not args.start_at_beginning:
        print("Click Restart to return to the beginning of the session.")
    print("Use Stop server in the dashboard to close this viewer.")
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
