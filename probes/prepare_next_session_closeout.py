"""Attended, fail-closed next-session build, r0 publication and verification.

One existing Schwab password prompt is inherited from the dated production
builder. No secret storage, silent provider retry or production modification
outside the selected next-session output/journal is introduced.

This is an EXPERIMENTAL candidate, not an installed production command.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from probes.verify_next_session_readiness import inspect_preparation


ET = ZoneInfo("America/New_York")


def run_closeout_preparation(root: Path, closed: date, next_day: date, *,
                             clock=lambda: datetime.now(ET),
                             run=subprocess.run) -> bool:
    """Return readiness; never print or imply full current-session audit PASS."""
    now = clock().astimezone(ET)
    if next_day <= closed:
        raise ValueError("Next session must follow completed session")
    if now.date() != closed or now.hour < 16:
        raise ValueError(
            "Post-close build requires 16:00 ET through midnight on source session")
    workflow_dir = root / "output" / "daily_universe_production" / (
        f"{next_day.isoformat()}-from-{closed.isoformat()}")
    journal_root = root / "output" / (
        f"quote_observation_journal_v2_api_opening_{next_day.isoformat()}")
    print("NEXT-SESSION PREPARATION: INCOMPLETE UNTIL VERIFIED", flush=True)
    print(f"Source session {closed} / target session {next_day}", flush=True)

    # A fully completed run is read-only on retries. An incomplete existing
    # directory requires explicit recovery; never quietly overwrite evidence.
    if not workflow_dir.exists():
        cmd = [
            sys.executable, str(root / "probes" / "run_daily_universe_production.py"),
            "--session-date", closed.isoformat(),
            "--target-date", next_day.isoformat(),
            "--publish-journal-root", str(journal_root),
        ]
        print("Running dated build + opening-r0 publication (one password prompt).", flush=True)
        completed = run(cmd, cwd=root)
        if completed.returncode:
            print(f"NEXT-SESSION PREPARATION: INCOMPLETE (build/publish exit {completed.returncode})", flush=True)
            return False
    try:
        evidence = inspect_preparation(root=root, closed_date=closed,
            next_date=next_day, journal_root=journal_root)
    except Exception as exc:
        print(f"NEXT-SESSION PREPARATION: INCOMPLETE ({type(exc).__name__}: {exc})", flush=True)
        return False
    print("NEXT-SESSION PREPARATION: READY", flush=True)
    print(f"Opening Uni: {evidence['uni_members']}; r0: {evidence['r0_content_sha256']}", flush=True)
    print("CURRENT-SESSION AUDIT AND REPLAY REMAIN SEPARATE VERDICTS", flush=True)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--closed-session", type=date.fromisoformat, required=True)
    parser.add_argument("--next-session", type=date.fromisoformat, required=True)
    parser.add_argument("--market-data-root", type=Path,
                        default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        success = run_closeout_preparation(args.market_data_root.resolve(),
            args.closed_session, args.next_session)
    except (ValueError, OSError) as exc:
        print(f"NEXT-SESSION PREPARATION: INCOMPLETE ({type(exc).__name__}: {exc})", flush=True)
        return 2
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
