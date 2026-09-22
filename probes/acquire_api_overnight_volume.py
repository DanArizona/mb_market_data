"""Acquire immutable API-only Overnight Volume evidence for opening Uni."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import datetime
from pathlib import Path

from mb_market_data.api_overnight_volume import (
    ET,
    OV_WINDOW_END,
    acquire_api_overnight_volume,
    write_api_overnight_volume_artifacts,
)
from mb_market_data.opening_hierarchy import load_opening_proposal
from mb_market_data.sampling_membership import SamplingHierarchyRevision
from mb_tools.schwab_secure import (
    console_auth_callback,
    make_secure_schwab_client,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Acquire Schwab five-minute extended-hours candles and calculate "
            "API-only OV_DECISION for every symbol in opening Uni."
        )
    )
    parser.add_argument("--opening-proposal", required=True, type=Path)
    parser.add_argument("--request-interval", type=float, default=0.5)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--max-symbols",
        type=int,
        help="Optional bounded smoke test; never marks opening Uni complete.",
    )
    parser.add_argument("--ecfg")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument(
        "--output-root",
        default="output/api_overnight_volume",
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


def validate_run_time(
    opening: SamplingHierarchyRevision,
    now_et: datetime,
) -> None:
    session_date = opening.session_date
    effective_at = opening.effective_at
    if now_et.date() != session_date:
        raise ValueError(
            f"opening session is {session_date}, but current ET date is "
            f"{now_et.date()}"
        )
    earliest = datetime.combine(session_date, OV_WINDOW_END, tzinfo=ET)
    if now_et < earliest:
        raise ValueError(
            f"API OV acquisition cannot start before {earliest.isoformat()}"
        )
    if now_et >= effective_at:
        raise ValueError(
            f"API OV acquisition must start before {effective_at.isoformat()}"
        )


def main() -> int:
    args = parse_args()
    try:
        opening = load_opening_proposal(args.opening_proposal)
        now_et = datetime.now(ET)
        validate_run_time(opening, now_et)
        if args.request_interval < 0:
            raise ValueError("--request-interval cannot be negative")
        if args.max_attempts < 1:
            raise ValueError("--max-attempts must be at least 1")
        if args.max_symbols is not None and args.max_symbols < 1:
            raise ValueError("--max-symbols must be at least 1")
        ecfg_path = resolve_ecfg(args.ecfg)
    except (AttributeError, OSError, TypeError, ValueError) as error:
        print(
            f"API Overnight Volume ERROR: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    complete_opening_uni = args.max_symbols is None
    symbols = opening.uni_symbols
    if args.max_symbols is not None:
        symbols = symbols[: args.max_symbols]
    minimum_seconds = max(0, len(symbols) - 1) * args.request_interval
    print("API-only Overnight Volume acquisition")
    print("=" * 79)
    print(f"Session date       : {opening.session_date}")
    print("Decision window    : 00:00 <= candle start < 08:25 ET")
    print(f"Opening Uni        : {len(opening.uni_symbols):,}")
    print(f"Requested symbols  : {len(symbols):,}")
    print(f"Complete opening Uni: {'YES' if complete_opening_uni else 'NO (smoke)'}")
    print(f"Request interval   : {args.request_interval:.3f} seconds")
    print(f"Minimum run time   : {minimum_seconds / 60:.1f} minutes")
    print(f"Encrypted config   : {ecfg_path}")
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
        batch = acquire_api_overnight_volume(
            client,
            symbols,
            trade_date=opening.session_date,
            request_interval_seconds=args.request_interval,
            max_attempts=args.max_attempts,
        )
    except Exception as error:
        print(
            f"API Overnight Volume ERROR: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    finally:
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    run_stamp = batch.started_at_utc.astimezone(ET).strftime(
        "%Y-%m-%d-%H-%M-%S"
    )
    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else Path(args.output_root)
        / f"{run_stamp}-session-{opening.session_date.isoformat()}"
    )
    try:
        artifacts = write_api_overnight_volume_artifacts(
            output_dir,
            opening_proposal_path=args.opening_proposal,
            opening_content_sha256=opening.content_sha256,
            opening_uni_count=len(opening.uni_symbols),
            opening_effective_at=opening.effective_at,
            complete_opening_uni=complete_opening_uni,
            batch=batch,
        )
    except (OSError, TypeError, ValueError) as error:
        print(
            f"API Overnight Volume ERROR: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    print()
    print(f"Successful symbols : {batch.successful_count:,}")
    print(f"Failed symbols     : {batch.failed_count:,}")
    print(f"Completed at       : {batch.completed_at_utc.astimezone(ET).isoformat()}")
    print(f"Evidence manifest  : {artifacts.manifest}")
    if batch.failed_count:
        print(
            "API Overnight Volume: FAIL (see explicit symbol failures)",
            file=sys.stderr,
        )
        return 1
    if batch.completed_at_utc.astimezone(ET) >= opening.effective_at:
        print(
            "API Overnight Volume: FAIL (completed at/after opening effective time)",
            file=sys.stderr,
        )
        return 1
    print("API Overnight Volume: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
