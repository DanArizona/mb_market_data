"""Check required production polling coverage at closeout, not merely journal integrity.

An untouched opening journal can pass a structural audit with zero poll runs;
that must NEVER qualify as a completed daily polling session.
This probe is read-only and never repairs missing observations.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import sys

PROJECT_ROOT=Path(__file__).resolve().parents[1]
SRC_ROOT=PROJECT_ROOT/"src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0,str(SRC_ROOT))

from mb_market_data.quote_journal_audit import audit_quote_journal
from mb_market_data.equity_closeout_calendar import require_exchange_session

ET=ZoneInfo("America/New_York")


def require_configured_session(report, closed_date: date, *,
                               expected_uni: int | None = None,
                               expected_focus: int | None = None) -> None:
    """Require Uni/Focus configured for the ACTUAL scheduled session length."""
    session = require_exchange_session(closed_date)
    session_minutes = (
        session.closes_at.hour * 60 + session.closes_at.minute - (9 * 60 + 30)
    )
    if session_minutes <= 0:
        raise ValueError("Invalid exchange close duration")
    expected_uni = expected_uni if expected_uni is not None else session_minutes * 2
    expected_focus = expected_focus if expected_focus is not None else session_minutes * 4
    if report.session_date != closed_date.isoformat() or report.schema_version != 2:
        raise ValueError("Daily journal session or schema differs")
    if report.membership_contract != "nested-uni-focus-hot-v1":
        raise ValueError("Daily journal membership contract differs")
    recorded = {run.channel for run in report.runs}
    if not {"uni", "focus"} <= recorded:
        raise ValueError("Required Uni/Focus poll runs were not registered")
    channels={channel.channel:channel for channel in report.channels}
    for name, expected in (("uni",expected_uni),("focus",expected_focus)):
        if name not in channels:
            raise ValueError(f"Required channel {name} is absent")
        actual=channels[name]
        if actual.expected_slot_count != expected:
            raise ValueError(
                f"{name} configured {actual.expected_slot_count} slots; expected {expected}")
        if actual.revision_count < 1:
            raise ValueError(f"{name} has no membership revisions")
        accounted=(actual.acquisition_count+actual.skipped_slot_count+
                   len(actual.missing_slot_ids))
        if accounted != expected:
            raise ValueError(f"{name} has inconsistent slot accounting")


def audit_completed_session(journal: Path, closed_date: date, *,
                            clock=lambda:datetime.now(ET)):
    now=clock().astimezone(ET)
    end=require_exchange_session(closed_date).closes_at+timedelta(seconds=90)
    if now<end:
        raise ValueError("Session close and 90-second slot grace have not elapsed")
    report=audit_quote_journal(journal)
    require_configured_session(report, closed_date)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("journal",type=Path)
    p.add_argument("--session-date",type=date.fromisoformat,required=True)
    args=p.parse_args()
    try:
        report=audit_completed_session(args.journal,args.session_date)
    except Exception as error:
        print(f"CURRENT SESSION AUDIT: ERROR ({type(error).__name__}: {error})")
        return 2
    print(f"CURRENT SESSION AUDIT: {'PASS' if report.passed else 'FAIL'}")
    for channel in report.channels:
        if channel.channel not in ("uni","focus","hot"):
            continue
        print(f"  {channel.channel}: {channel.acquisition_count}/{channel.expected_slot_count} "
              f"acquisitions; missing {len(channel.missing_slot_ids)}; "
              f"skipped {channel.skipped_slot_count}")
        for slot in channel.missing_slot_ids[:20]:
            print(f"    missing {slot}")
    if report.findings:
        for finding in report.findings:
            print(f"  [{finding.code}] {finding.detail}")
    return 0 if report.passed else 1


if __name__=="__main__":
    raise SystemExit(main())
