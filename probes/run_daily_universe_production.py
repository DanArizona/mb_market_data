"""Run the complete immutable post-close daily universe workflow."""

from __future__ import annotations

import argparse
import shlex
import sys
from datetime import date
from pathlib import Path

from mb_market_data.daily_universe_workflow import (
    build_stage_commands,
    run_stages,
    validate_workflow_dates,
    workflow_paths,
    write_workflow_manifest,
)


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
            "Acquire the Nasdaq directory and Schwab post-close snapshot, "
            "then build the next-session deterministic Uni roster."
        )
    )
    parser.add_argument("--session-date", required=True, type=date_argument)
    parser.add_argument("--target-date", required=True, type=date_argument)
    parser.add_argument(
        "--output-root",
        default="output/daily_universe_production",
    )
    parser.add_argument(
        "--output-dir",
        help="Explicit immutable workflow directory; overrides --output-root.",
    )
    parser.add_argument("--batch-size", type=int, default=400)
    parser.add_argument("--nasdaq-timeout", type=float, default=30.0)
    parser.add_argument("--schwab-timeout", type=int, default=30)
    parser.add_argument("--ecfg")
    parser.add_argument("--minimum-volume", default="10000")
    parser.add_argument("--minimum-close", default="0.10")
    parser.add_argument("--minimum-market-cap", default="4000000")
    parser.add_argument("--maximum-market-cap", default="40000000")
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Print the three commands without running or writing anything.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repository_root = Path(__file__).resolve().parents[1]
    default_name = (
        f"{args.target_date.isoformat()}-from-"
        f"{args.session_date.isoformat()}"
    )
    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else Path(args.output_root) / default_name
    )
    paths = workflow_paths(output_dir)

    try:
        validate_workflow_dates(args.session_date, args.target_date)
        stages = build_stage_commands(
            repository_root=repository_root,
            python_executable=sys.executable,
            paths=paths,
            session_date=args.session_date,
            target_date=args.target_date,
            batch_size=args.batch_size,
            nasdaq_timeout=args.nasdaq_timeout,
            schwab_timeout=args.schwab_timeout,
            ecfg=args.ecfg,
            minimum_volume=args.minimum_volume,
            minimum_close=args.minimum_close,
            minimum_market_cap=args.minimum_market_cap,
            maximum_market_cap=args.maximum_market_cap,
        )
        if args.plan:
            print("Daily universe production plan")
            print("=" * 79)
            print(f"Session date : {args.session_date}")
            print(f"Target date  : {args.target_date}")
            print(f"Output       : {paths.root}")
            for index, stage in enumerate(stages, start=1):
                print(f"\n[{index}/{len(stages)}] {stage.name}")
                print(shlex.join(stage.command))
            return 0

        if paths.root.exists():
            raise FileExistsError(
                f"workflow output already exists: {paths.root}"
            )

        print("Daily universe production flow")
        print("=" * 79)
        print(f"Session date : {args.session_date}")
        print(f"Target date  : {args.target_date}")
        print(f"Output       : {paths.root}")
        run_stages(stages, repository_root=repository_root)
        manifest = write_workflow_manifest(
            paths=paths,
            stages=stages,
            session_date=args.session_date,
            target_date=args.target_date,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(
            f"Daily universe production ERROR: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    print("\nDaily universe production: PASS")
    print("=" * 79)
    print(f"Uni watchlist : {paths.universe / 'uni_watchlist.csv'}")
    print(f"Decision ledger: {paths.universe / 'decision_ledger.csv'}")
    print(f"Manifest      : {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
