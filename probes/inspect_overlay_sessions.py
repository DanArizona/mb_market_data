"""Inspect discoverable dated daily-flow journals without modifying production.

Identity validation only; reported controller phase/timestamp is *saved*, never a
live health determination. Does not contact a provider or launch an Overlay.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mb_market_data.overlay_session_catalog import (
    SessionDiscoveryError, catalog_sessions, inspect_session
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-data-root", required=True, type=Path)
    parser.add_argument("--date", help="Validate one selected ET trading date (YYYY-MM-DD)")
    args = parser.parse_args()
    try:
        if args.date:
            result = {"session": inspect_session(args.market_data_root, args.date).to_dict()}
        else:
            sessions, rejected = catalog_sessions(args.market_data_root)
            result = {
                "available_sessions": [item.to_dict() for item in sessions],
                "rejected_candidates": rejected,
            }
        print(json.dumps(result, indent=2))
        print("READ ONLY: identity validation; saved status is not proof of a live controller.")
        return 0
    except SessionDiscoveryError as error:
        print(f"SESSION NOT ATTACHABLE: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
