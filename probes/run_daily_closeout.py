"""Attended one-command daily closeout, with independent explicit verdicts.

EXPERIMENTAL. Uses the accepted post-close snapshot workflow (one interactive
password prompt), then read-only current-session audit and replay. No live
poller/Health controls, automatic credentials or production installation.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4


def combine_verdicts(preparation_code: int, audit_code: int,
                     replay_code: int) -> tuple[str, int]:
    """A genuine audit FAIL is retained; it is never converted to PASS."""
    if preparation_code != 0 or audit_code not in (0, 1) or replay_code != 0:
        return "INCOMPLETE", 2
    if audit_code == 1:
        return "RECORDED_WITH_FINDINGS", 1
    return "VERIFIED_COMPLETE", 0


def run_daily_closeout(root: Path, closed_date: date, next_date: date, *,
                       runner=subprocess.run,
                       clock=lambda: datetime.now(timezone.utc),
                       receipt_root: Path | None = None) -> dict:
    if next_date <= closed_date:
        raise ValueError("Next session must follow completed session")
    root = Path(root).resolve()
    today_journal = root / "output" / (
        f"quote_observation_journal_v2_api_opening_{closed_date.isoformat()}") / (
        f"{closed_date.isoformat()}.sqlite3")
    programs = (
        ("next_session", [
            sys.executable, str(root / "probes" / "prepare_next_session_closeout.py"),
            "--closed-session", closed_date.isoformat(),
            "--next-session", next_date.isoformat(),
            "--market-data-root", str(root)]),
        ("current_audit", [
            sys.executable, str(root / "probes" / "audit_required_session_channels.py"),
            str(today_journal), "--session-date", closed_date.isoformat()]),
        ("current_replay", [
            sys.executable, str(root / "probes" / "replay_quote_observation_journal.py"),
            str(today_journal), "--progress-every", "0"]),
    )
    codes = {}
    print("DAILY CLOSEOUT: NOT VERIFIED UNTIL ALL STAGES FINISH", flush=True)
    for index, (label, command) in enumerate(programs, start=1):
        print(f"\n[{index}/3] {label}", flush=True)
        try:
            codes[label] = int(runner(command, cwd=root).returncode)
        except (OSError, ValueError) as error:
            print(f"{label} ERROR: {type(error).__name__}: {error}", flush=True)
            codes[label] = 2
        print(f"{label.upper()} EXIT CODE: {codes[label]}", flush=True)
        # Always inspect current-session evidence, even if preparation fails.
    verdict, exit_code = combine_verdicts(codes["next_session"],
        codes["current_audit"], codes["current_replay"])
    receipt = {
        "contract": "daily-closeout-two-verdict-v1",
        "closed_session": closed_date.isoformat(),
        "next_session": next_date.isoformat(),
        "recorded_at_utc": clock().astimezone(timezone.utc).isoformat(),
        "next_session_preparation": (
            "READY" if codes["next_session"] == 0 else "INCOMPLETE"),
        "current_session_audit": (
            "PASS" if codes["current_audit"] == 0 else
            "FAIL" if codes["current_audit"] == 1 else "ERROR"),
        "current_session_replay": (
            "COMPLETE" if codes["current_replay"] == 0 else "ERROR"),
        "stages_return_codes": codes,
        "verdict": verdict,
        "exit_code": exit_code,
        "health_monitor_shutdown": "NOT_MANAGED__OPERATOR_MUST_STOP_WITH_Q",
        "production_changes": "only existing dated build and r0 publish; no poller control",
    }
    output_dir = (Path(receipt_root) if receipt_root is not None else
                  root / "output" / "production_closeout" /
                  closed_date.isoformat())
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / (
        clock().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") +
        "-" + uuid4().hex[:8] + ".json")
    with result_path.open("x", encoding="utf-8") as out:
        json.dump(receipt, out, indent=2, sort_keys=True)
        out.write("\n")
    receipt["receipt_path"] = str(result_path)
    print("\nCLOSEOUT SUMMARY", flush=True)
    print(f"CURRENT SESSION AUDIT: {receipt['current_session_audit']}", flush=True)
    print(f"CURRENT SESSION REPLAY: {receipt['current_session_replay']}", flush=True)
    print(f"NEXT SESSION PREPARATION: {receipt['next_session_preparation']}", flush=True)
    print(f"CLOSEOUT: {verdict}", flush=True)
    print(f"Evidence receipt: {result_path}", flush=True)
    print("Stop the independent Health monitor/watchdog with Q and confirm exit.", flush=True)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--closed-session", required=True, type=date.fromisoformat)
    parser.add_argument("--next-session", required=True, type=date.fromisoformat)
    parser.add_argument("--market-data-root", type=Path,
                        default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        receipt = run_daily_closeout(args.market_data_root,
            args.closed_session, args.next_session)
    except Exception as error:
        print(f"CLOSEOUT: INCOMPLETE ({type(error).__name__}: {error})")
        return 2
    return receipt["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
