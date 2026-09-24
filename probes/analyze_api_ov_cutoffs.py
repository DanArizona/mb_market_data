"""Acquire and compare retrospective API OV cutoff landmarks."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import datetime
from pathlib import Path

from mb_market_data.api_overnight_volume import (
    ET,
    acquire_api_overnight_volume,
)
from mb_market_data.opening_hierarchy import load_opening_proposal
from mb_market_data.ov_cutoff_analysis import (
    ANALYSIS_WINDOW_END,
    CUTOFFS,
    analyze_cutoff_batch,
    load_production_ov_baseline,
    write_cutoff_analysis_artifacts,
)
from mb_market_data.sampling_membership import SamplingHierarchyRevision
from mb_tools.schwab_secure import (
    console_auth_callback,
    make_secure_schwab_client,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Retrospectively acquire 00:00-09:30 ET Schwab candles and "
            "compare OV ranks at 08:25, 09:00, 09:15, 09:25, and OV_FINAL."
        )
    )
    parser.add_argument("--opening-proposal", required=True, type=Path)
    parser.add_argument("--production-ov-manifest", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--request-interval", type=float, default=0.5)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--ecfg")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument(
        "--output-root",
        default="output/api_ov_cutoff_analysis",
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
    """Require the complete 09:25-09:30 candle to have closed."""

    if now_et < opening.effective_at:
        raise ValueError(
            "cutoff analysis cannot start before opening effective time "
            f"{opening.effective_at.isoformat()}"
        )


def main() -> int:
    args = parse_args()
    try:
        opening = load_opening_proposal(args.opening_proposal)
        validate_run_time(opening, datetime.now(ET))
        if args.limit < 1:
            raise ValueError("--limit must be at least 1")
        if args.request_interval < 0:
            raise ValueError("--request-interval cannot be negative")
        if args.max_attempts < 1:
            raise ValueError("--max-attempts must be at least 1")
        baseline = load_production_ov_baseline(
            args.production_ov_manifest,
            opening,
        )
        ecfg_path = resolve_ecfg(args.ecfg)
    except (AttributeError, OSError, TypeError, ValueError) as error:
        print(
            f"API OV cutoff analysis ERROR: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    symbols = opening.uni_symbols
    minimum_seconds = max(0, len(symbols) - 1) * args.request_interval
    print("API Overnight Volume cutoff analysis")
    print("=" * 79)
    print(f"Session date       : {opening.session_date}")
    print("Acquisition window : 00:00 <= candle start < 09:30 ET")
    print(
        "Cutoffs           : "
        + "  ".join(cutoff.strftime("%H:%M") for _, cutoff in CUTOFFS)
    )
    print(f"Opening Uni        : {len(symbols):,}")
    print(f"Selection limit    : {args.limit:,}")
    print(f"Production baseline: {baseline.manifest_path}")
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
        print("Encrypted configuration accepted.", flush=True)
        batch = acquire_api_overnight_volume(
            client,
            symbols,
            trade_date=opening.session_date,
            window_end=ANALYSIS_WINDOW_END,
            request_interval_seconds=args.request_interval,
            max_attempts=args.max_attempts,
        )
    except Exception as error:
        print(
            f"API OV cutoff analysis ERROR: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    finally:
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    try:
        analysis = analyze_cutoff_batch(
            batch,
            opening_symbols=symbols,
            baseline=baseline,
            limit=args.limit,
        )
        run_stamp = batch.started_at_utc.astimezone(ET).strftime(
            "%Y-%m-%d-%H-%M-%S"
        )
        output_dir = (
            Path(args.output_dir)
            if args.output_dir
            else Path(args.output_root)
            / f"{run_stamp}-session-{opening.session_date.isoformat()}"
        )
        artifacts = write_cutoff_analysis_artifacts(
            output_dir,
            opening_proposal_path=args.opening_proposal,
            opening=opening,
            baseline=baseline,
            batch=batch,
            analysis=analysis,
        )
    except (OSError, TypeError, ValueError) as error:
        print(
            f"API OV cutoff analysis ERROR: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    print()
    print(f"Successful symbols : {batch.successful_count:,}")
    print(f"Failed symbols     : {batch.failed_count:,}")
    baseline_match_count = sum(
        item.baseline_match is True for item in analysis.metrics
    )
    print(
        f"{baseline.cutoff_et.strftime('%H:%M')} baseline    : "
        f"{baseline_match_count:,}/{len(symbols):,} match"
    )
    for name, cutoff in CUTOFFS:
        print(
            f"Top {args.limit:>3} at {cutoff.strftime('%H:%M')} : "
            + " ".join(analysis.membership[name])
        )
    print()
    print("Membership transitions")
    for transition in analysis.transitions:
        entrants = " ".join(transition["entrants"]) or "none"
        exits = " ".join(transition["exits"]) or "none"
        print(
            f"  {transition['from']} -> {transition['to']}: "
            f"overlap={transition['overlap_count']}/"
            f"{len(analysis.membership[transition['from']])}"
        )
        print(f"    entrants: {entrants}")
        print(f"    exits   : {exits}")
    print()
    print(f"Metrics CSV        : {artifacts.metrics}")
    print(f"Membership         : {artifacts.membership}")
    print(f"Candle evidence    : {artifacts.candles}")
    print(f"Manifest           : {artifacts.manifest}")

    if batch.failed_count:
        print(
            "API OV cutoff analysis: FAIL (explicit symbol failures)",
            file=sys.stderr,
        )
        return 1
    if analysis.baseline_mismatch_symbols:
        print(
            "API OV cutoff analysis: FAIL (production baseline mismatch: "
            + " ".join(analysis.baseline_mismatch_symbols)
            + ")",
            file=sys.stderr,
        )
        return 1
    print("API OV cutoff analysis: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
