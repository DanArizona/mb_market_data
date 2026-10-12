"""Real-process synthetic contention checks. No API, credentials or production DB."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mb_market_data.quote_journal_audit import audit_quote_journal
from mb_market_data.quote_journal_replay import QuoteJournalReplayReader
from mb_market_data.quote_observation_store import PollRunProvenance, QuoteObservationStore
from mb_market_data.sampling_membership import SamplingHierarchyRevision
from mb_market_data.schwab_quotes import QuoteBatchResult, QuoteResult, QuoteStatus
from mb_market_data.sqlite_write_recovery import WriteRetryExhausted
from mb_market_data.watchlist_polling import PollRequest, PollSlot, WatchlistKind


def save(path, value):
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def wait_for(predicate, timeout, detail):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    raise RuntimeError("Synthetic check timed out: " + detail)


def worker(case, role):
    if not case.name.startswith("synthetic_"):
        raise ValueError("Only a newly allocated synthetic case may be opened")
    info = json.loads((case / "CASE.json").read_text())
    if info.get("synthetic") is not True:
        raise ValueError("Synthetic marker missing")
    start = datetime.fromisoformat(info["start"])
    journal = case / "synthetic.sqlite3"
    if role.startswith("lock"):
        with closing(sqlite3.connect(journal)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            (case / (role + ".ready")).touch()
            wait_for(lambda: (case / (role + ".release")).exists(), 15, role + " release")
            connection.rollback()
        return 0
    events = case / (role + ".diagnostics.jsonl")
    def notify(value):
        with events.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if value["state"] == "RETRYING":
            (case / (role + ".waiting")).touch()
    store = QuoteObservationStore(journal, session_date=start.date(), schema_version=2, write_event_handler=notify)
    generated, committed = 0, 0
    for sample, second in enumerate((0, 30) if role == "uni" else (5, 20), 1):
        if sample == 2:
            wait_for(lambda: (case / (role + ".next")).exists(), 15, role + " next sample")
        when = start + timedelta(seconds=second)
        symbols = ("AAPL", "MSFT", "SPY") if role == "uni" else ("MSFT",)
        request = PollRequest(PollSlot(WatchlistKind(role), when), 0, symbols, when + timedelta(milliseconds=1))
        completed = when + timedelta(milliseconds=20)
        result = QuoteBatchResult(tuple(QuoteResult(
            symbol, QuoteStatus.QUOTE, {"symbol": symbol, "quote": {"lastPrice": 10.5, "totalVolume": 100}},
            None, 1, request.dispatched_at, completed,
        ) for symbol in symbols), 1, 400, ())
        generated += 1
        with (case / (role + ".acquired.jsonl")).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"synthetic": True, "acquisition_id": request.batch_id,
                                    "slot_id": request.slot_id, "dispatched_at": request.dispatched_at.isoformat(),
                                    "completed_at": completed.isoformat(), "symbols": symbols}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            store.record_acquisition(role, request, result, completed_at=completed)
        except WriteRetryExhausted as error:
            save(case / (role + ".receipt.json"), {"state": "FAILED", "generated": generated,
                "committed": committed, "diagnostic": error.diagnostic})
            return 0  # this is the expected synthetic failure, recorded explicitly
        committed += 1
        (case / (role + ".committed." + str(sample))).touch()
    save(case / (role + ".receipt.json"), {"state": "COMPLETED", "generated": generated, "committed": committed})
    return 0


def verify_watchdog(case, start):
    """Kill only our inspector child; its supervisor and an unrelated process survive."""
    journal = case / "watchdog_journal" / f"{start.date()}.sqlite3"
    store = QuoteObservationStore(journal, session_date=start.date(), schema_version=2)
    store.initialize()
    with patch("mb_market_data.quote_observation_store._utc_now", return_value=start - timedelta(seconds=1)):
        store.record_membership_revision(SamplingHierarchyRevision(
            session_date=start.date(), revision=0, effective_at=start,
            uni_symbols=("MSFT",), focus_symbols=(), hot_symbols=(), source="SYNTHETIC watchdog",
        ))
    original = hashlib.sha256(journal.read_bytes()).hexdigest()
    directory = case / "output/polling_health" / str(start.date())
    log = (case / "watchdog.process.log").open("w", encoding="utf-8")
    supervisor = subprocess.Popen([sys.executable, str(Path(__file__).with_name("polling_health.py")),
        "monitor", "--market-data-root", str(case), "--journal", str(journal),
        "--session-date", str(start.date()), "--interval", "1", "--supervise", "--no-popup"],
        stdout=log, stderr=subprocess.STDOUT)
    companion = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        def state():
            try:
                return json.loads((directory / "supervisor.json").read_text())
            except (OSError, ValueError):
                return {}
        running = wait_for(lambda: state() if state().get("state") == "RUNNING" else None, 15, "monitor under watchdog")
        pid = running["child_pid"]
        # The fresh supervisor receipt identifies the exact inspector we own.
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(1, False, pid)
            if not handle:
                raise RuntimeError("Could not open our synthetic inspector child")
            try:
                if not kernel.TerminateProcess(handle, 99):
                    raise RuntimeError("Could not stop our synthetic inspector child")
            finally:
                kernel.CloseHandle(handle)
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
        wait_for(lambda: state().get("state") == "STOPPED", 5, "independent inspector failure detection")
        assert supervisor.poll() is None and companion.poll() is None
        (directory / "stop.request").touch()
        assert supervisor.wait(timeout=15) == 0
        assert companion.poll() is None
        assert hashlib.sha256(journal.read_bytes()).hexdigest() == original
        return "PASS: inspector exit detected; supervisor stayed alive; unrelated process and journal unchanged"
    finally:
        (directory / "stop.request").parent.mkdir(parents=True, exist_ok=True)
        (directory / "stop.request").touch()
        if supervisor.poll() is None:
            try:
                supervisor.wait(timeout=12)
            except subprocess.TimeoutExpired:
                supervisor.terminate()
                supervisor.wait(timeout=3)
        companion.terminate()
        companion.wait(timeout=3)
        log.close()


def verify(report_root):
    report_root.mkdir(parents=True, exist_ok=True)
    case = report_root / ("synthetic_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8])
    case.mkdir()
    start = datetime.now(ZoneInfo("America/New_York")).replace(hour=9, minute=30, second=0, microsecond=0)
    save(case / "CASE.json", {"synthetic": True, "start": start.isoformat()})
    journal = case / "synthetic.sqlite3"
    store = QuoteObservationStore(journal, session_date=start.date(), schema_version=2)
    store.initialize()
    with patch("mb_market_data.quote_observation_store._utc_now", return_value=start - timedelta(seconds=1)):
        store.record_membership_revision(SamplingHierarchyRevision(
            session_date=start.date(), revision=0, effective_at=start,
            uni_symbols=("AAPL", "MSFT", "SPY"), focus_symbols=("MSFT",), hot_symbols=(), source="SYNTHETIC contention",
        ))
    for channel in ("uni", "focus"):
        store.record_run(PollRunProvenance(channel, start, "synthetic-contention", {
            "watchlist_kind": channel, "poll_window_start_et": start.isoformat(),
            "poll_seconds": [0, 30] if channel == "uni" else [5, 20, 35, 50],
            "poll_window_end_et": (start + timedelta(minutes=1)).isoformat(),
        }))
    children = []
    logs = []
    def launch(role):
        stream = (case / (role + ".process.log")).open("w", encoding="utf-8")
        logs.append(stream)
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", role, "--case", str(case)],
                                 stdout=stream, stderr=subprocess.STDOUT)
        children.append(child)
        return child
    try:
        lock = launch("lock1")
        wait_for(lambda: (case / "lock1.ready").exists(), 5, "temporary writer lock")
        uni, focus = launch("uni"), launch("focus")
        wait_for(lambda: all((case / (c + ".waiting")).exists() for c in ("uni", "focus")), 4, "both writers retrying")
        (case / "lock1.release").touch()
        lock.wait(timeout=3)
        wait_for(lambda: all((case / (c + ".committed.1")).exists() for c in ("uni", "focus")), 5, "both recovered commits")
        lock = launch("lock2")
        wait_for(lambda: (case / "lock2.ready").exists(), 5, "persistent writer lock")
        (case / "focus.next").touch()
        focus.wait(timeout=12)
        failed = json.loads((case / "focus.receipt.json").read_text())
        assert failed["state"] == "FAILED" and failed["committed"] == 1 and failed["generated"] == 2, failed
        assert failed["diagnostic"]["state"] == "EXHAUSTED", failed
        assert failed["diagnostic"]["attempt_started_elapsed_seconds"] < 5, failed
        assert failed["diagnostic"]["attempts"] <= 20, failed
        assert uni.poll() is None, "Uni process was affected by Focus's failed write"
        (case / "lock2.release").touch()
        lock.wait(timeout=3)
        (case / "uni.next").touch()
        uni.wait(timeout=5)
        continued = json.loads((case / "uni.receipt.json").read_text())
        assert continued == {"state": "COMPLETED", "generated": 2, "committed": 2}, continued
        audit = audit_quote_journal(journal, audited_at=start + timedelta(minutes=1))
        assert all(finding.code == "missing_poll_slots" for finding in audit.findings), audit.findings
        assert audit.table_counts["quote_acquisition"] == 3 and audit.table_counts["quote_observation"] == 7
        assert len(next(c for c in audit.channels if c.channel == "focus").missing_slot_ids) == 3
        sequences = []
        for _ in range(2):
            reader = QuoteJournalReplayReader(journal)
            sequences.append(hashlib.sha256("\n".join(repr(e) for e in reader.events()).encode()).hexdigest())
        assert sequences[0] == sequences[1]
        report = {"synthetic": True, "result": "PASS", "temporary_contention": "both writers recovered",
                  "persistent_contention": "Focus stopped within budget; acquired evidence retained",
                  "healthy_channel": "Uni process continued and committed its next sample",
                  "acquisitions": 3, "observations": 7, "replays_agree": True,
                  "focus_acquired_samples": 2, "focus_committed_samples": 1,
                  "configured_slot_coverage": "INCOMPLETE", "focus_missing_slots": 3,
                  "production_api_calls": 0, "production_journals_opened": 0,
                  "focus_failure": failed["diagnostic"], "case": str(case)}
        report["monitor_watchdog"] = verify_watchdog(case, start)
        save(case / "verification.json", report)
        print("REAL-PROCESS SQLITE CONTENTION: PASS", flush=True)
        print("Temporary lock recovered both writers. Persistent lock stopped Focus within its budget.")
        print("Uni continued. Failed Focus evidence remains saved; three Focus gaps remain visible.")
        print("Structural checks passed; audit reports expected incomplete coverage; two replays agree.")
        print("Monitor watchdog: inspector exit detected; independent processes and journal unchanged.")
        print(f"Synthetic report: {case / 'verification.json'}")
        return report
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=3)
        for stream in logs:
            stream.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-root", type=Path)
    parser.add_argument("--worker", choices=("uni", "focus", "lock1", "lock2"), help=argparse.SUPPRESS)
    parser.add_argument("--case", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        raise SystemExit(worker(args.case, args.worker))
    if args.report_root is None:
        parser.error("--report-root is required")
    verify(args.report_root)
