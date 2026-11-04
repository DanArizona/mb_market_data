"""Read-only, Eastern-time post-close readiness reminder (experimental).

Intended for an opt-in *interactive* Windows Task Scheduler task. No API,
password, poller control, journal writes or automatic closeout launch.
When the dated next-session evidence is not READY after actual close + 10 min, show an
operator reminder. A reminder never declares closeout success.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

PROJECT_ROOT=Path(__file__).resolve().parents[1]
SRC_ROOT=PROJECT_ROOT/"src"
for path in (str(PROJECT_ROOT),str(SRC_ROOT)):
    if path not in sys.path:
        sys.path.insert(0,path)

from probes.verify_next_session_readiness import inspect_preparation
from mb_market_data.closeout_notifications import PushoverStub, due_notice
from mb_market_data.equity_closeout_calendar import (
    equity_session, next_exchange_session, reminder_due_at,
)

ET=ZoneInfo("America/New_York")


@dataclass(frozen=True)
class ReminderDecision:
    status: str
    source_date: date
    explanation: str


def inspect_due(root: Path, *, now: datetime | None = None) -> ReminderDecision:
    """Only remind for the exchange's actual session after close + ten minutes."""
    now = (now or datetime.now(ET)).astimezone(ET)
    source = now.date()
    session = equity_session(source)
    if not session.is_open:
        return ReminderDecision("NOT_DUE", source,
            f"No U.S. equity trading session on {source}: {session.reason}")
    due_at = reminder_due_at(source)
    if now < due_at:
        return ReminderDecision("NOT_DUE", source,
            f"Exchange closes at {session.close_time:%H:%M} ET; "
            f"first reminder eligibility {due_at:%H:%M} ET")
    journal = root / "output" / (
        f"quote_observation_journal_v2_api_opening_{source.isoformat()}"
    ) / f"{source.isoformat()}.sqlite3"
    if not journal.is_file():
        return ReminderDecision("NO_SESSION", source,
            "No dated production journal found; no live session inferred")
    target = next_exchange_session(source)
    directory = root / "output" / "daily_universe_production" / (
        f"{target.isoformat()}-from-{source.isoformat()}"
    )
    if not directory.is_dir():
        return ReminderDecision("DUE", source,
            f"Today's production journal exists, but no dated next-session "
            f"workflow exists for {target.isoformat()}")
    journal_root = root / "output" / (
        f"quote_observation_journal_v2_api_opening_{target.isoformat()}"
    )
    try:
        inspect_preparation(root=root, closed_date=source, next_date=target,
            journal_root=journal_root, workflow_dir=directory)
    except Exception as exc:
        return ReminderDecision("DUE", source,
            f"Next-session workflow for {target.isoformat()} is not verified: "
            f"{type(exc).__name__}: {exc}")
    return ReminderDecision("READY", source,
        f"Next-session preparation READY for {target.isoformat()}")


def display_notification(decision: ReminderDecision):
    if sys.platform!="win32":
        raise RuntimeError("Windows interactive desktop required")
    from ctypes import windll
    message=(
        f"Post-close preparation is INCOMPLETE for {decision.source_date}.\n\n"
        f"{decision.explanation}\n\n"
        "In your production sea-green CMD, run:\n"
        "python probes\\attended_closeout_today.py\n\n"
        "Enter the actual NEXT TRADING DATE when prompted "
        "(check exchange market holidays).\n\n"
        "The workflow will prompt once for your encrypted Schwab configuration. "
        "Do not wait until after Eastern midnight to acquire today's snapshot."
    )
    windll.user32.MessageBoxW(
        None,message,"mb_package — POST-CLOSE ACTION REQUIRED",
        0x00000030 | 0x00040000)  # warning, topmost, acknowledgment only


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-data-root",type=Path,default=PROJECT_ROOT)
    parser.add_argument("--notify",action="store_true",
                        help="Show Windows desktop warning only when preparation is DUE")
    parser.add_argument("--as-of",type=datetime.fromisoformat,
                        help="Read-only historical check with explicit timezone-aware timestamp")
    args=parser.parse_args()
    if args.as_of is not None:
        if args.as_of.tzinfo is None or args.as_of.utcoffset() is None:
            parser.error("--as-of requires an explicit UTC offset")
        if args.notify:
            parser.error("--as-of cannot be combined with --notify")
    root=args.market_data_root.resolve()
    # A reminder in an isolated experiment must not be redirected to a
    # production checkout before approved installation.
    if args.notify and root != PROJECT_ROOT.resolve():
        print("REMINDER BLOCKED: notify requires the installed repository checkout")
        return 2
    try:
        decision=inspect_due(root,now=args.as_of)
    except Exception as exc:
        print(f"REMINDER CHECK ERROR: {type(exc).__name__}: {exc}")
        return 2
    print(f"CLOSEOUT REMINDER: {decision.status} ({decision.source_date})")
    print(decision.explanation)
    if args.notify and decision.status=="DUE":
        # Future Pushover integration point; never send in this candidate.
        delivery=PushoverStub().dispatch(
            due_notice(decision.source_date.isoformat(), decision.explanation))
        print(f"PUSHOVER: {delivery.outcome} (no push sent)", flush=True)
        display_notification(decision)
        return 0  # Desktop notification succeeded; readiness remains DUE.
    return 1 if decision.status=="DUE" else 0


if __name__=="__main__":
    raise SystemExit(main())
