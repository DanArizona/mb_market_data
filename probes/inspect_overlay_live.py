"""Show exactly what is committed in a validated daily journal, read-only.

This is a bounded live-attachment data check, not the interactive Real Time
Overlay, a liveness assertion, a full journal audit, or a membership editor.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mb_market_data.overlay_live_attachment import ReadOnlySessionObserver
from mb_market_data.overlay_session_catalog import SessionDiscoveryError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-data-root", required=True, type=Path)
    parser.add_argument("--date", required=True, help="Exact ET date YYYY-MM-DD")
    args = parser.parse_args()
    try:
        observer = ReadOnlySessionObserver(args.market_data_root, args.date)
        progress = observer.observe()
    except (SessionDiscoveryError, OSError, ValueError) as error:
        print(f"READ-ONLY LIVE ATTACHMENT REJECTED: {error}", file=sys.stderr)
        return 2
    print(json.dumps(progress.to_dict(), indent=2))
    print("READ ONLY: committed journal evidence; saved phase is not process liveness.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
