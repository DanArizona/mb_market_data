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
    equity_session, next_exchange_session, previous_exchange_session, reminder_due_at,
)

ET=ZoneInfo("America/New_York")


@dataclass(frozen=True)
class ReminderDecision:
    status: str
    source_date: date
    explanation: str


def _journal_for(root: Path, day: date) -> Path:
    return root / "output" / (
        f"quote_observation_journal_v2_api_opening_{day.isoformat()}"
    ) / f"{day.isoformat()}.sqlite3"


def _readiness(root: Path, source: date, target: date,
               *, missing_status: str) -> ReminderDecision:
    directory = root / "output" / "daily_universe_production" / (
        f"{target.isoformat()}-from-{source.isoformat()}"
    )
    if not directory.is_dir():
        return ReminderDecision(missing_status, source,
            f"Source-session journal exists, but no dated next-session workflow "
            f"exists for {target.isoformat()}")
    journal_root = root / "output" / (
        f"quote_observation_journal_v2_api_opening_{target.isoformat()}"
    )
    try:
        inspect_preparation(root=root, closed_date=source, next_date=target,
            journal_root=journal_root, workflow_dir=directory)
    except Exception as exc:
        return ReminderDecision(missing_status, source,
            f"Next-session workflow for {target.isoformat()} is not verified: "
            f"{type(exc).__name__}: {exc}")
    return ReminderDecision("READY", source,
        f"Next-session preparation READY for {target.isoformat()}")


def _prior_session_recovery(root: Path, today: date) -> ReminderDecision | None:
    """Carry a missed source-date closeout into the next trading morning."""
    source = previous_exchange_session(today)
    if not _journal_for(root, source).is_file():
        return None
    result = _readiness(root, source, today, missing_status="RECOVERY_REQUIRED")
    return result if result.status == "RECOVERY_REQUIRED" else None


def inspect_due(root: Path, *, now: datetime | None = None) -> ReminderDecision:
    """Warn after the scheduled close and about misses next trading morning.

    The Windows scheduler may run hourly all day; this is the actual date/time
    and readiness gate. Every time comparison uses America/New_York, not the
    local machine's civil clock.
    """
    now = (now or datetime.now(ET)).astimezone(ET)
    source = now.date()
    session = equity_session(source)
    if not session.is_open:
        return ReminderDecision("NOT_DUE", source,
            f"No U.S. equity trading session on {source}: {session.reason}")
    due_at = reminder_due_at(source)
    if now < due_at:
        # If the previous trading session has a journal but no confirmed
        # next-session opening, tomorrow morning must not silently hide it.
        if now.time() >= time(7, 0):
            recovery = _prior_session_recovery(root, source)
            if recovery is not None:
                return recovery
        return ReminderDecision("NOT_DUE", source,
            f"Exchange closes at {session.close_time:%H:%M} ET; "
            f"first reminder eligibility {due_at:%H:%M} ET")
    if not _journal_for(root, source).is_file():
        recovery = _prior_session_recovery(root, source)
        if recovery is not None:
            return recovery
        return ReminderDecision("NO_SESSION", source,
            "No dated production journal found; no live session inferred")
    target = next_exchange_session(source)
    return _readiness(root, source, target, missing_status="DUE")


def display_notification(decision: ReminderDecision):
    if sys.platform != "win32":
        raise RuntimeError("Windows interactive desktop required")
    from ctypes import windll
    if decision.status == "RECOVERY_REQUIRED":
        target = next_exchange_session(decision.source_date)
        message = (
            f"MISSED POST-CLOSE PREPARATION for {decision.source_date}.\n\n"
            f"{decision.explanation}\n\n"
            "The original source-date snapshot can no longer be acquired. "
            "Do not relabel today's quote volume as yesterday's.\n\n"
            "Review the recovery runbook. In production CMD, inspect with:\n"
            "python probes\\assess_closeout_recovery.py "
            f"--closed-session {decision.source_date} "
            f"--next-session {target}\n\n"
            "An older verified Uni roster may be considered only through "
            "the explicitly approved, provenance-labeled recovery process."
        )
        title = "mb_package — MISSED CLOSEOUT: RECOVERY REQUIRED"
    else:
        message = (
            f"Post-close preparation is INCOMPLETE for {decision.source_date}.\n\n"
            f"{decision.explanation}\n\n"
            "In your production sea-green CMD, run:\n"
            "python probes\\attended_closeout_today.py\n\n"
            "Confirm the next exchange trading date. The snapshot stage "
            "will request your encrypted Schwab password once. Complete "
            "the same-day snapshot before Eastern midnight."
        )
        title = "mb_package — POST-CLOSE ACTION REQUIRED"
    windll.user32.MessageBoxW(
        None, message, title, 0x00000030 | 0x00040000)



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
    if args.notify and decision.status in {"DUE", "RECOVERY_REQUIRED"}:
        # Dormant Phase 2 hook: no credentials or network requests.
        if decision.status == "DUE":
            delivery=PushoverStub().dispatch(
                due_notice(decision.source_date.isoformat(), decision.explanation))
            print(f"PUSHOVER: {delivery.outcome} (no push sent)", flush=True)
        display_notification(decision)
        return 0  # Desktop warning displayed; readiness verdict unchanged.
    return 1 if decision.status in {"DUE", "RECOVERY_REQUIRED"} else 0


if __name__=="__main__":
    raise SystemExit(main())
