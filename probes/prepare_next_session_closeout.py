"""Attended next-session closeout preparation: build -> publish -> verify.

EXPERIMENTAL CANDIDATE. Interactive credential entry is inherited from the
existing production builder; this does not schedule or silently authenticate.
No existing source artifacts are overwritten, no failed receipt is relabeled.
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import subprocess
import sys

from verify_next_session_readiness import inspect_preparation


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--closed-session",type=date.fromisoformat,required=True)
    p.add_argument("--next-session",type=date.fromisoformat,required=True)
    p.add_argument("--market-data-root",type=Path,default=Path(__file__).resolve().parents[1])
    args=p.parse_args()
    root=args.market_data_root.resolve()
    closed,next_day=args.closed_session,args.next_session
    if next_day<=closed:
        p.error("next-session must be after closed-session")
    manifest=root/"output"/"daily_universe_production"/(
        f"{next_day.isoformat()}-from-{closed.isoformat()}")/"workflow_manifest.json"
    proposal=manifest.parent/"opening_hierarchy_r0.json"
    journal_root=root/"output"/f"quote_observation_journal_v2_api_opening_{next_day.isoformat()}"
    journal=journal_root/f"{next_day.isoformat()}.sqlite3"
    print("CLOSEOUT PREPARATION: INCOMPLETE UNTIL VERIFIED",flush=True)
    print(f"Completed session: {closed}; next session: {next_day}",flush=True)
    # A saved candidate is never implicitly overwritten. If there is a
    # complete prior build, inspect and publish that exact proposal instead.
    if not manifest.exists():
        cmd=[sys.executable,str(root/"probes"/"run_daily_universe_production.py"),
             "--session-date",closed.isoformat(),"--target-date",next_day.isoformat()]
        print("Building next-session Universe using dated post-close evidence.",flush=True)
        status=subprocess.run(cmd,cwd=root).returncode
        if status:
            print(f"NEXT-SESSION PREPARATION: INCOMPLETE (build exit {status})",flush=True)
            return status
    if not proposal.is_file():
        print("NEXT-SESSION PREPARATION: INCOMPLETE (missing opening proposal)",flush=True)
        return 2
    cmd=[sys.executable,str(root/"probes"/"publish_sampling_hierarchy.py"),
         str(journal),str(proposal)]
    status=subprocess.run(cmd,cwd=root).returncode
    if status:
        print(f"NEXT-SESSION PREPARATION: INCOMPLETE (publication exit {status})",flush=True)
        return status
    try:
        result=inspect_preparation(root=root,closed_date=closed,next_date=next_day,
                    journal_root=journal_root)
    except Exception as exc:
        print(f"NEXT-SESSION PREPARATION: INCOMPLETE ({type(exc).__name__}: {exc})",flush=True)
        return 2
    print(f"NEXT-SESSION PREPARATION: READY; Uni {result['uni_members']}",flush=True)
    print("CURRENT-SESSION AUDIT/REPLAY VERDICT STILL REQUIRED SEPARATELY",flush=True)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
