"""Build an explainable next-session universe from frozen close evidence."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from mb_market_data.daily_universe import (
    UniverseFilterConfig,
    decide_daily_universe,
    read_csv_rows,
    write_daily_universe_artifacts,
)


def decimal_argument(value: str) -> Decimal:
    try:
        result = Decimal(value.replace(",", "").strip())
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError(f"invalid decimal: {value!r}") from error

    if not result.is_finite():
        raise argparse.ArgumentTypeError(f"invalid decimal: {value!r}")
    return result


def date_argument(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}; expected YYYY-MM-DD"
        ) from error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Finalize frozen Nasdaq and Schwab close evidence into a "
            "deterministic next-session Uni and per-symbol decision ledger."
        )
    )
    parser.add_argument(
        "--symbol-directory",
        required=True,
        help="normalized_all.csv from probe_nasdaq_symbol_directory.py",
    )
    parser.add_argument(
        "--market-data",
        required=True,
        help=(
            "enriched_candidates_direct_market_cap.csv from "
            "probe_daily_universe_direct_market_cap.py"
        ),
    )
    parser.add_argument("--session-date", required=True, type=date_argument)
    parser.add_argument("--target-date", required=True, type=date_argument)
    parser.add_argument(
        "--output-dir",
        help=(
            "New output directory. Default: "
            "output/daily_universe/TARGET-from-SESSION"
        ),
    )
    parser.add_argument(
        "--minimum-volume",
        type=decimal_argument,
        default=Decimal("10000"),
    )
    parser.add_argument(
        "--minimum-close",
        type=decimal_argument,
        default=Decimal("0.10"),
    )
    parser.add_argument(
        "--minimum-market-cap", type=decimal_argument, default=Decimal("4000000")
    )
    parser.add_argument(
        "--maximum-market-cap", type=decimal_argument, default=Decimal("40000000")
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    output_dir = Path(args.output_dir) if args.output_dir else (
        Path("output")
        / "daily_universe"
        / f"{args.target_date.isoformat()}-from-{args.session_date.isoformat()}"
    )

    try:
        config = UniverseFilterConfig(
            minimum_volume=args.minimum_volume,
            minimum_close=args.minimum_close,
            minimum_market_cap=args.minimum_market_cap,
            maximum_market_cap=args.maximum_market_cap,
        )
        source_rows = read_csv_rows(args.symbol_directory)
        market_rows = read_csv_rows(args.market_data)
        decisions = decide_daily_universe(source_rows, market_rows, config)
        paths = write_daily_universe_artifacts(
            output_dir=output_dir,
            decisions=decisions,
            symbol_directory_path=args.symbol_directory,
            market_data_path=args.market_data,
            session_date=args.session_date,
            target_date=args.target_date,
            config=config,
        )
    except (OSError, ValueError) as error:
        print(
            f"Daily universe build ERROR: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    included = sum(decision.included for decision in decisions)
    reason_counts: Counter[str] = Counter()
    for decision in decisions:
        reason_counts.update(decision.reason_codes.split(";"))

    print("Daily universe build: PASS")
    print("=" * 79)
    print(f"Session date      : {args.session_date.isoformat()}")
    print(f"Target date       : {args.target_date.isoformat()}")
    print(f"Source symbols    : {len(decisions):,}")
    print(f"Included          : {included:,}")
    print(f"Rejected          : {len(decisions) - included:,}")
    print(f"Decision ledger   : {paths['decision_ledger']}")
    print(f"Uni watchlist     : {paths['uni_watchlist']}")
    print(f"Manifest          : {paths['manifest']}")
    print()
    print("Reason counts")
    for reason, count in reason_counts.most_common():
        print(f"  {reason:30s} {count:8,d}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
