"""Build a strict schema-v2 opening r0 proposal from a daily universe."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mb_market_data.opening_hierarchy import (
    build_opening_hierarchy,
    write_opening_proposal,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert a completed daily-universe directory into a strict "
            "schema-v2 opening hierarchy r0 proposal."
        )
    )
    parser.add_argument(
        "universe_dir",
        type=Path,
        help="Directory containing manifest.json and uni_symbols.csv.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Proposal path. Default: opening_hierarchy_r0.json inside "
            "the universe directory."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output or args.universe_dir / "opening_hierarchy_r0.json"
    try:
        revision = build_opening_hierarchy(args.universe_dir)
        write_opening_proposal(output, revision)
    except (OSError, TypeError, ValueError) as error:
        print(
            f"Opening hierarchy build ERROR: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    print("Opening sampling hierarchy: PASS")
    print("=" * 79)
    print(f"Session date     : {revision.session_date}")
    print(f"Revision         : r{revision.revision}")
    print(f"Effective at     : {revision.effective_at.isoformat()}")
    print(f"Uni symbols      : {len(revision.uni_symbols):,}")
    print(f"Focus symbols    : {len(revision.focus_symbols):,}")
    print(f"Hot symbols      : {len(revision.hot_symbols):,}")
    print(f"Content SHA-256  : {revision.content_sha256}")
    print(f"Proposal         : {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
