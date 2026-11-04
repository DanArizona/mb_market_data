"""Attended three-stage closeout and strictly read-only historical verification.

ATTENDED: dated next-session production workflow (one Schwab authentication),
full-session audit, full event replay, and immutable result receipt.
VERIFY ONLY: no provider calls and no production writes; qualify existing
evidence from an isolated checkout before authorizing installation.

Independent evidence must never be relabeled: legitimate missing slots are an
audit FAIL, not a replay failure or a false PASS.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4


SCRIPT_ROOT = Path(__file__).resolve().parents[1]


def combine_verdicts(preparation_code: int, audit_code: int,
                     replay_code: int) -> tuple[str, int]:
    """Return completed, recorded-with-findings, or incomplete, without laundering FAIL."""
    if preparation_code != 0 or audit_code not in (0, 1) or replay_code != 0:
        return "INCOMPLETE", 2
    if audit_code == 1:
        return "RECORDED_WITH_FINDINGS", 1
    return "VERIFIED_COMPLETE", 0


def run_daily_closeout(root: Path, closed_date: date, next_date: date, *,
                       program_root: Path | None = None,
                       verify_only: bool = False,
                       runner=subprocess.run,
                       clock=lambda: datetime.now(timezone.utc),
                       receipt_root: Path | None = None) -> dict:
    """Run one deterministic closeout; no actual provider work in verify-only mode."""
    if next_date <= closed_date:
        raise ValueError("Next session must follow completed session")
    root = Path(root).resolve()
    scripts = Path(program_root or SCRIPT_ROOT).resolve()
    if not verify_only and scripts != root:
        raise ValueError(
            "Attended mode requires installed program root == market data root. "
            "Use --verify-only for isolated verification of production evidence."
        )
    today_journal = root / "output" / (
        f"quote_observation_journal_v2_api_opening_{closed_date.isoformat()}") / (
        f"{closed_date.isoformat()}.sqlite3")

    # Different executable paths are intentional: the isolated checkout's
    # verification logic reads the actual journal under --market-data-root.
    if verify_only:
        first_stage = [
            sys.executable, str(scripts / "probes" / "verify_next_session_readiness.py"),
            "--closed-session", closed_date.isoformat(),
            "--next-session", next_date.isoformat(),
            "--market-data-root", str(root)]
    else:
        first_stage = [
            sys.executable, str(scripts / "probes" / "prepare_next_session_closeout.py"),
            "--closed-session", closed_date.isoformat(),
            "--next-session", next_date.isoformat(),
            "--market-data-root", str(root)]
    programs = (
        ("next_session", first_stage),
        ("current_audit", [
            sys.executable, str(scripts / "probes" / "audit_required_session_channels.py"),
            str(today_journal), "--session-date", closed_date.isoformat()]),
        ("current_replay", [
            sys.executable, str(scripts / "probes" / "replay_quote_observation_journal.py"),
            str(today_journal), "--progress-every", "0"]),
    )
    print(
        "DAILY CLOSEOUT: VERIFY EXISTING EVIDENCE (NO PROVIDER/PRODUCTION WRITES)"
        if verify_only else
        "DAILY CLOSEOUT: ATTENDED PREPARATION (NOT VERIFIED UNTIL ALL STAGES FINISH)",
        flush=True,
    )
    print(f"Source session {closed_date}; next session {next_date}", flush=True)
    codes = {}
    for index, (label, command) in enumerate(programs, start=1):
        print(f"\n[{index}/3] {label}", flush=True)
        try:
            codes[label] = int(runner(command, cwd=scripts).returncode)
        except (OSError, ValueError) as error:
            print(f"{label} ERROR: {type(error).__name__}: {error}", flush=True)
            codes[label] = 2
        print(f"{label.upper()} EXIT CODE: {codes[label]}", flush=True)
        # Never skip the current audit/replay after a failed next-session stage.
    verdict, exit_code = combine_verdicts(
        codes["next_session"], codes["current_audit"], codes["current_replay"])
    stamp = clock().astimezone(timezone.utc)
    receipt = {
        "contract": "daily-closeout-two-verdict-v1",
        "mode": "VERIFY_ONLY" if verify_only else "ATTENDED",
        "closed_session": closed_date.isoformat(),
        "next_session": next_date.isoformat(),
        "market_data_root": str(root),
        "program_root": str(scripts),
        "recorded_at_utc": stamp.isoformat(),
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
        "production_writes": (
            "NONE__READ_ONLY_EVIDENCE" if verify_only else
            "ONLY_EXISTING_DATED_WORKFLOW_AND_OPENING_R0_PUBLICATION"),
    }
    # Even read-only verification writes its RECEIPT only under the experimental
    # program checkout by default, never in the production data root.
    output_dir = (
        Path(receipt_root) if receipt_root is not None else
        scripts / "output" / "closeout_verification" / closed_date.isoformat()
        if verify_only else
        root / "output" / "production_closeout" / closed_date.isoformat()
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / (
        stamp.strftime("%Y%m%dT%H%M%S%fZ") +
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
    print("Health monitor/watchdog shutdown is separate: stop with Q.", flush=True)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--closed-session", required=True, type=date.fromisoformat)
    parser.add_argument("--next-session", required=True, type=date.fromisoformat)
    parser.add_argument("--market-data-root", type=Path, default=SCRIPT_ROOT)
    parser.add_argument("--verify-only", action="store_true",
                        help="No snapshot build or publication; read existing evidence only.")
    parser.add_argument("--receipt-root", type=Path,
                        help="Optional explicit directory for immutable JSON receipts.")
    args = parser.parse_args()
    try:
        receipt = run_daily_closeout(
            args.market_data_root, args.closed_session, args.next_session,
            verify_only=args.verify_only, receipt_root=args.receipt_root)
    except Exception as error:
        print(f"CLOSEOUT: INCOMPLETE ({type(error).__name__}: {error})")
        return 2
    return receipt["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
