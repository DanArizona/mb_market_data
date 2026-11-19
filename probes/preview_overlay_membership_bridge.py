"""Standalone offline demo of Manual Membership inside the Overlay workspace.

This preview uses a disposable synthetic membership worker and an independent
fabricated historical replay fixture. No real provider, credentials, installed
controller, or production journals are read or changed.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from mb_market_data.daily_operator_demo import DemoSession
from mb_market_data.local_dashboard_server import LocalDashboardServer
from mb_market_data.quote_dashboard import create_quote_dashboard
from test_quote_dashboard import overlay_controller


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8062)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("Use a local port from 1024 through 65535")

    with TemporaryDirectory(prefix="mb_overlay_membership_SYNTHETIC_") as temporary:
        demo = DemoSession(Path(temporary))
        stop = Event()
        try:
            demo.start()
            replay = overlay_controller()
            replay.finish()
            app = create_quote_dashboard(
                replay, request_server_stop=stop.set,
                synthetic_operator_root=demo.root,
            )
            server = LocalDashboardServer(
                app.server, host="127.0.0.1", port=args.port, stop_event=stop
            )
            print("OFFLINE SYNTHETIC OVERLAY MEMBERSHIP QA", flush=True)
            print("No Schwab, no real session, no production journal.", flush=True)
            print(f"Temporary synthetic session: {demo.root}", flush=True)
            print(f"Open Firefox: http://127.0.0.1:{args.port}", flush=True)
            print("Select Real Time (synthetic QA), then Membership.", flush=True)
            print("Preview and confirm INTC Hot -> Focus; then Focus -> Hot.", flush=True)
            print("Use Stop server in the Overlay to shut down and clean up.", flush=True)
            server.serve_until_stopped()
        finally:
            demo.close()
    print("Synthetic worker stopped; temporary journal removed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
