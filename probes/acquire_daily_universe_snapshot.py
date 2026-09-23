"""Acquire a ToS-independent frozen post-close universe snapshot."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mb_market_data.daily_universe import read_csv_rows
from mb_market_data.daily_universe_snapshot import (
    ACQUISITION_TIMING_POLICY,
    VOLUME_SEMANTICS,
    build_market_data_snapshot,
    validate_acquisition_time,
    validate_batch_acquisition_time,
    write_acquisition_json,
    write_snapshot_csv,
    write_snapshot_manifest,
)
from mb_market_data.schwab_quotes import (
    DEFAULT_QUOTE_BATCH_SIZE,
    fetch_quotes_batched,
)
from mb_tools.schwab_secure import (
    console_auth_callback,
    make_secure_schwab_client,
)


ET = ZoneInfo("America/New_York")


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
            "Acquire post-close regular price, consolidated volume, and "
            "shares outstanding for a Nasdaq-derived candidate universe."
        )
    )
    parser.add_argument(
        "--candidate-csv",
        required=True,
        help="candidate_non_etf_non_test.csv from the Nasdaq directory run.",
    )
    parser.add_argument("--session-date", required=True, type=date_argument)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_QUOTE_BATCH_SIZE,
    )
    parser.add_argument(
        "--max-symbols",
        type=int,
        help="Optional bounded smoke-test limit.",
    )
    parser.add_argument("--ecfg")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument(
        "--output-root",
        default="output/daily_universe_snapshot",
    )
    parser.add_argument(
        "--output-dir",
        help="Explicit immutable output directory; overrides --output-root.",
    )
    return parser.parse_args()


def resolve_ecfg(explicit_path: str | None) -> Path:
    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path).expanduser())
    environment_path = os.environ.get("MB_SCHWAB_ECFG")
    if environment_path:
        candidates.append(Path(environment_path).expanduser())
    vault = os.environ.get("MB_VAULT")
    if vault:
        candidates.append(Path(vault).expanduser() / "secure_schwabdev.ecfg")
    candidates.append(Path.cwd() / "secure_schwabdev.ecfg")
    for path in candidates:
        if path.is_file():
            return path.resolve()
    searched = "\n".join(f"  {path}" for path in candidates)
    raise FileNotFoundError(
        "Could not find secure_schwabdev.ecfg.\n"
        f"Paths checked:\n{searched}"
    )


def main() -> int:
    args = parse_args()
    try:
        candidate_path = Path(args.candidate_csv)
        candidates = read_csv_rows(candidate_path)
        if args.max_symbols is not None:
            if args.max_symbols < 1:
                raise ValueError("--max-symbols must be at least 1")
            candidates = candidates[: args.max_symbols]
        ecfg_path = resolve_ecfg(args.ecfg)
        validated_at = datetime.now(ET)
        validate_acquisition_time(args.session_date, validated_at)
    except (OSError, ValueError) as error:
        print(
            f"Daily universe snapshot ERROR: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    symbols = [str(row.get("symbol") or "") for row in candidates]
    print("Daily universe post-close snapshot")
    print("=" * 79)
    print(f"Session date     : {args.session_date}")
    print(f"Candidates       : {len(symbols):,}")
    print(f"Batch size       : {args.batch_size}")
    print(f"Snapshot gate    : {ACQUISITION_TIMING_POLICY}")
    print(f"Validated at ET  : {validated_at.isoformat()}")
    print(f"Volume semantics : {VOLUME_SEMANTICS}")
    print(f"Encrypted config : {ecfg_path}")
    print()

    password = getpass.getpass("Encrypted config password: ")
    client = None
    try:
        client = make_secure_schwab_client(
            ecfg_path,
            password,
            timeout=args.timeout,
            call_on_auth=console_auth_callback,
        )
        print("Encrypted configuration accepted.", flush=True)
        acquisition = fetch_quotes_batched(
            client,
            symbols,
            fields="all",
            batch_size=args.batch_size,
        )
        validate_batch_acquisition_time(args.session_date, acquisition)
        rows = build_market_data_snapshot(
            candidates,
            acquisition,
            session_date=args.session_date,
        )
    except Exception as error:
        print(
            f"Daily universe snapshot ERROR: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    finally:
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    run_stamp = datetime.now(ET).strftime("%Y-%m-%d-%H-%M-%S")
    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else Path(args.output_root)
        / f"{run_stamp}-session-{args.session_date.isoformat()}"
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    snapshot_path = output_dir / "market_data_snapshot.csv"
    acquisition_path = output_dir / "quote_acquisition.json"
    manifest_path = output_dir / "manifest.json"

    write_snapshot_csv(snapshot_path, rows)
    write_acquisition_json(
        acquisition_path,
        acquisition,
        session_date=args.session_date,
    )
    write_snapshot_manifest(
        manifest_path,
        session_date=args.session_date,
        candidate_path=candidate_path,
        snapshot_path=snapshot_path,
        acquisition_path=acquisition_path,
        acquisition=acquisition,
        rows=rows,
        validated_at=validated_at,
    )

    statuses = Counter(row["acquisition_status"] for row in rows)
    session_matches = Counter(
        row["regular_market_session_match"] or "missing" for row in rows
    )
    print()
    print("Daily universe snapshot: PASS")
    print("=" * 79)
    print(f"Candidates       : {len(rows):,}")
    print(f"Requests         : {acquisition.request_count:,}")
    print(
        "Statuses         : "
        + ", ".join(f"{key}={value:,}" for key, value in sorted(statuses.items()))
    )
    print(
        "Session matches  : "
        + ", ".join(
            f"{key}={value:,}" for key, value in sorted(session_matches.items())
        )
    )
    print(f"Market snapshot  : {snapshot_path}")
    print(f"Raw acquisition  : {acquisition_path}")
    print(f"Manifest         : {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
