"""Read-only missed closeout classification; never invent a late snapshot.

Operator supplies explicit source and intended target trading dates. The
diagnostic reports what saved evidence exists and identifies when the
completed-session acquisition window has been irretrievably missed. It
does not create a fallback roster, publish r0 or contact any data provider.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo
import sys

PROJECT_ROOT=Path(__file__).resolve().parents[1]
SRC_ROOT=PROJECT_ROOT/"src"
for candidate in (str(PROJECT_ROOT),str(SRC_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0,candidate)
from probes.verify_next_session_readiness import inspect_preparation
from mb_market_data.equity_closeout_calendar import (
    require_exchange_session, validate_next_exchange_session,
)

ET=ZoneInfo("America/New_York")


@dataclass(frozen=True,slots=True)
class RecoveryAssessment:
    state: str
    source_session: str
    target_session: str
    retained_snapshot_manifest: bool
    workflow_manifest: bool
    detail: str


def assess(root: Path, source: date, target: date,
           *, now: datetime | None = None) -> RecoveryAssessment:
    if target<=source:
        raise ValueError("Target must be later than source date")
    validate_next_exchange_session(source, target)
    session=require_exchange_session(source)
    root=Path(root)
    now=(now or datetime.now(ET)).astimezone(ET)
    directory=(root/"output"/"daily_universe_production"/
               f"{target.isoformat()}-from-{source.isoformat()}")
    snapshot=(directory/"market_snapshot"/"manifest.json").is_file()
    workflow=(directory/"workflow_manifest.json").is_file()
    journal_root=(root/"output"/
                  f"quote_observation_journal_v2_api_opening_{target.isoformat()}")
    try:
        evidence=inspect_preparation(root=root,closed_date=source,
            next_date=target,journal_root=journal_root)
    except Exception as exc:
        detail=f"{type(exc).__name__}: {exc}"
    else:
        return RecoveryAssessment(
            "READY",source.isoformat(),target.isoformat(),
            snapshot,workflow,
            f"Already verified: {evidence['uni_members']} Uni. No recovery needed.")
    if now.date()>source:
        reason=("A saved snapshot manifest may permit separately qualified "
                "reconstruction from retained dated evidence; inspect its "
                "contents and acquisition time. "
                if snapshot else
                "No completed-session snapshot manifest is present. ")
        reason += (
            "The source date is over: do NOT acquire fresh Schwab volume and "
            "mislabel it as the prior session. Record a missed-closeout "
            "exception. Review a previously verified Uni roster for an "
            "explicitly authorized, provenance-labeled carry-forward; publish "
            "only prospectively and keep the original readiness failure."
        )
        return RecoveryAssessment("RECOVERY_REQUIRED",source.isoformat(),
            target.isoformat(),snapshot,workflow,reason+" Original check: "+detail)
    if now.date()==source and now>=session.closes_at:
        return RecoveryAssessment("DUE_BEFORE_ET_MIDNIGHT",
            source.isoformat(),target.isoformat(),snapshot,workflow,
            "The source calendar date has not ended. Acquire valid post-close "
            "data promptly using an attended, authorized procedure. "
            "Do not delete/overwrite a partial immutable workflow. "+detail)
    return RecoveryAssessment("NOT_YET_DUE",source.isoformat(),
        target.isoformat(),snapshot,workflow,
        "The reviewed scheduled exchange close has not yet occurred. "+detail)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--closed-session",required=True,type=date.fromisoformat)
    parser.add_argument("--next-session",required=True,type=date.fromisoformat)
    parser.add_argument("--market-data-root",type=Path,default=Path.cwd())
    args=parser.parse_args()
    try:
        result=assess(args.market_data_root.resolve(),
            args.closed_session,args.next_session)
    except Exception as exc:
        print(f"CLOSEOUT RECOVERY: ERROR ({type(exc).__name__}: {exc})")
        return 2
    print(f"CLOSEOUT RECOVERY: {result.state}")
    print(f"Source/target: {result.source_session} / {result.target_session}")
    print(f"Retained snapshot manifest: {result.retained_snapshot_manifest}")
    print(f"Complete workflow manifest: {result.workflow_manifest}")
    print(result.detail)
    return 0 if result.state=="READY" else 1


if __name__=="__main__":
    raise SystemExit(main())
