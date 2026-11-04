"""Operator entrypoint for one attended, single-authentication daily closeout.

EXPERIMENTAL. Does not guess the next trading date (market holidays differ
from weekdays). The operator selects it, confirms, then the existing
combined closeout takes over. No password is collected in this script.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
from mb_market_data.equity_closeout_calendar import (
    next_exchange_session, require_postclose_source_time,
    validate_next_exchange_session,
)

PROJECT_ROOT=Path(__file__).resolve().parents[1]
ET=ZoneInfo("America/New_York")


def run_operator_closeout(*, root: Path, closed_date: date,
                          now: datetime,
                          ask=input, run=subprocess.run) -> int:
    """One date selection + confirmation; then a single attended closeout."""
    now=now.astimezone(ET)
    if closed_date != now.date():
        raise ValueError("Fresh closeout requires today's ET source trading date")
    require_postclose_source_time(closed_date, now)
    journal=(root/"output"/
        f"quote_observation_journal_v2_api_opening_{closed_date}"/
        f"{closed_date}.sqlite3")
    if not journal.is_file():
        raise ValueError(f"No session journal found: {journal}")
    print(f"Completed-session date (ET): {closed_date}",flush=True)
    print("Choose the next REAL EXCHANGE TRADING SESSION. "
          "Check market holidays and early closures; do not guess from weekdays.",
          flush=True)
    expected_target = next_exchange_session(closed_date)
    value=ask(f"Next trading session (YYYY-MM-DD) [{expected_target}]: ").strip()
    if not value:
        value = expected_target.isoformat()
    try:
        target=date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Invalid next-session date; expected YYYY-MM-DD") from exc
    if target<=closed_date or target.weekday()>=5:
        raise ValueError("Next session must be later than source and not a weekend")
    validate_next_exchange_session(closed_date, target)
    print(f"PLAN: {closed_date} post-close snapshot -> {target} opening r0, "
          "then current journal audit + replay.",flush=True)
    print("You will enter your encrypted Schwab configuration password once "
          "when the snapshot stage prompts.",flush=True)
    confirmation=ask("Type YES to start the attended closeout: ").strip()
    if confirmation!="YES":
        print("CLOSEOUT: NOT STARTED — no data or provider action",flush=True)
        return 2
    command=[
        sys.executable,str(root/"probes"/"run_daily_closeout.py"),
        "--closed-session",closed_date.isoformat(),
        "--next-session",target.isoformat(),
        "--market-data-root",str(root)]
    return int(run(command,cwd=root).returncode)


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-data-root",type=Path,default=PROJECT_ROOT)
    args=parser.parse_args()
    root=args.market_data_root.resolve()
    if root!=PROJECT_ROOT.resolve():
        print("CLOSEOUT BLOCKED: attended code and repository root must match")
        return 2
    now=datetime.now(ET)
    try:
        return run_operator_closeout(root=root,closed_date=now.date(),now=now)
    except (EOFError, KeyboardInterrupt):
        print("\nCLOSEOUT: CANCELLED")
        return 2
    except (ValueError, OSError) as exc:
        print(f"CLOSEOUT: NOT STARTED ({type(exc).__name__}: {exc})")
        return 2


if __name__=="__main__":
    raise SystemExit(main())
