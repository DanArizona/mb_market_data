"""Interactive offline preview of the experimental historical Overlay shell.

Uses only the existing synthetic unit-test fixture. No journal files,
credentials, provider calls, or production state are opened or modified.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from threading import Event

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from mb_market_data.local_dashboard_server import LocalDashboardServer
from mb_market_data.quote_dashboard import create_quote_dashboard
from test_quote_dashboard import overlay_controller


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8061)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("Use a local port between 1024 and 65535")

    controller = overlay_controller()
    controller.finish()
    stop = Event()
    app = create_quote_dashboard(controller, request_server_stop=stop.set)
    server = LocalDashboardServer(
        app.server, host="127.0.0.1", port=args.port, stop_event=stop
    )
    print("SYNTHETIC OVERLAY WORKSPACE — OFFLINE, READ-ONLY")
    print("No journal, credentials, provider calls, or production writes.")
    print(f"Open http://127.0.0.1:{args.port} in a local browser.")
    print("Select Charts to inspect the example OHLCV, volume and quote Overlay.")
    print("Ctrl+C or Stop server terminates this preview only.")
    server.serve_until_stopped()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
