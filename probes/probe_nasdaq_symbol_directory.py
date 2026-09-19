"""Freeze and normalize the current Nasdaq Trader symbol directory."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mb_market_data.nasdaq_symbol_directory import (
    NASDAQ_LISTED_URL,
    OTHER_LISTED_URL,
    build_symbol_directory_snapshot,
    fetch_directory,
    write_symbol_directory_artifacts,
)


ET = ZoneInfo("America/New_York")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Acquire Nasdaq-listed and other-exchange-listed symbol "
            "directories and normalize them for daily universe selection."
        )
    )
    parser.add_argument("--nasdaq-url", default=NASDAQ_LISTED_URL)
    parser.add_argument("--other-url", default=OTHER_LISTED_URL)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--output-root",
        default="output/nasdaq_symbol_directory",
    )
    parser.add_argument(
        "--output-dir",
        help="Explicit immutable output directory; overrides --output-root.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    retrieved_at = datetime.now(ET)
    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else Path(args.output_root)
        / retrieved_at.strftime("%Y-%m-%d-%H-%M-%S")
    )

    print("Nasdaq Symbol Directory Probe")
    print("=" * 72)
    print(f"Retrieved ET : {retrieved_at.isoformat()}")
    print(f"Output       : {output_dir.resolve()}")
    print()

    try:
        print("Fetching Nasdaq-listed securities...")
        nasdaq_raw = fetch_directory(args.nasdaq_url, timeout=args.timeout)
        print(f"  received {len(nasdaq_raw):,} bytes")
        print("Fetching other-exchange-listed securities...")
        other_raw = fetch_directory(args.other_url, timeout=args.timeout)
        print(f"  received {len(other_raw):,} bytes")
        snapshot = build_symbol_directory_snapshot(nasdaq_raw, other_raw)
        paths = write_symbol_directory_artifacts(
            output_dir=output_dir,
            snapshot=snapshot,
            retrieved_at_et=retrieved_at,
            nasdaq_url=args.nasdaq_url,
            other_url=args.other_url,
        )
    except (OSError, UnicodeError, ValueError) as error:
        print(
            f"Nasdaq symbol directory ERROR: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    rows = snapshot.normalized_rows
    source_counts = Counter(row["source"] for row in rows)
    exchange_counts = Counter(row["listing_exchange_name"] for row in rows)
    market_counts = Counter(
        row["market_category"] for row in rows if row["market_category"]
    )
    financial_counts = Counter(
        row["financial_status"] for row in rows if row["financial_status"]
    )

    print()
    print("Source file metadata")
    print("-" * 72)
    print(
        "nasdaqlisted : File Creation Time: "
        + snapshot.nasdaq_file_creation_time
    )
    print(
        "otherlisted  : File Creation Time: "
        + snapshot.other_file_creation_time
    )
    print()
    print("Record counts")
    print("-" * 72)
    print(f"Nasdaq listed              : {source_counts['nasdaqlisted']:6d}")
    print(f"Other exchange listed      : {source_counts['otherlisted']:6d}")
    print(f"Combined                   : {len(rows):6d}")
    print(f"Test issues                : {sum(r['test_issue'] == 'Y' for r in rows):6d}")
    print(f"ETFs                       : {sum(r['etf'] == 'Y' for r in rows):6d}")
    print(f"Preliminary candidates     : {len(snapshot.candidate_rows):6d}")

    print("\nListing exchange counts\n" + "-" * 23)
    for name, count in sorted(exchange_counts.items()):
        print(f"{name:20s} {count:6d}")
    print("\nNasdaq market-category counts\n" + "-" * 31)
    for name, count in sorted(market_counts.items()):
        print(f"{name:20s} {count:6d}")
    print("\nNasdaq financial-status counts\n" + "-" * 31)
    for name, count in sorted(financial_counts.items()):
        print(f"{name:20s} {count:6d}")

    print("\nEvidence files\n" + "-" * 72)
    print(f"Raw Nasdaq  : {paths['nasdaq_raw'].resolve()}")
    print(f"Raw other   : {paths['other_raw'].resolve()}")
    print(f"Normalized  : {paths['normalized'].resolve()}")
    print(f"Candidates  : {paths['candidates'].resolve()}")
    print(f"Manifest    : {paths['manifest'].resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
