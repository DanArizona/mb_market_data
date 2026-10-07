"""Isolated real-process acceptance of polling supervision. No API clients.

The packet imports reviewed journal code from --market-data-root/src, while
all mutable paths belong to a newly created SYNTHETIC case directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

try:
    import acquisition_health as health
    import polling_health as cli
except ModuleNotFoundError:
    from mb_market_data import acquisition_health as health
    import importlib.util
    spec = importlib.util.spec_from_file_location("polling_health", Path(__file__).with_name("polling_health.py"))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

PACKET = Path(__file__).resolve().parent
STUB = '''# SYNTHETIC ONLY. No API imports or credential access.
import json, sys
from pathlib import Path
case = Path(__file__).resolve().parents[1]
settings = json.loads((case / "case.json").read_text(encoding="utf-8"))
sys.path.insert(0, settings["packet"])
from synthetic_session import poller_main
raise SystemExit(poller_main(case))
'''


def now():
    return datetime.now(health.UTC)


def fingerprint(path):
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def validate(root):
    for manifest, base in (("PACKET_MANIFEST.json", PACKET), ("trusted_source_files.json", root)):
        for name, expected in json.loads((PACKET / manifest).read_text())["sha256"].items():
            if fingerprint(base / name) != expected:
                raise ValueError(f"File differs from its reviewed version: {base / name}")
    sys.path.insert(0, str(root / "src"))


def load_case(case):
    case = case.resolve()
    data = json.loads((case / "case.json").read_text(encoding="utf-8"))
    root = Path(data["source_root"]).resolve()
    expected_parent = root / "output/polling_health_live_test"
    if (data.get("synthetic") is not True or case.parent != expected_parent
            or not case.name.startswith("case_") or Path(data["packet"]).resolve() != PACKET
            or data.get("case") != str(case)):
        raise ValueError("Only this packet's separate SYNTHETIC case is allowed")
    validate(root)
    if (case / "probes/probe_universe_quote_watch.py").read_text() != STUB:
        raise ValueError("Synthetic poller launcher differs from its reviewed version")
    start, stop = health.timestamp(data["start"]), health.timestamp(data["stop"])
    session = start.astimezone(health.ET).date()
    journal = case / "output/journal" / f"{session}.sqlite3"
    return data, health.HealthConfig(case, journal, session, start, stop, grace_seconds=5)


def latest_case():
    return Path(json.loads((PACKET / "LATEST_CASE.json").read_text())["case"])


def title(value):
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(value)


def inventory():
    if os.name == "nt":
        return health.windows_process_inventory()
    # Local Linux validation inspects actual /proc command lines. Windows
    # acceptance always uses the unmodified native CIM inspector.
    processes = []
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            argv = [x.decode() for x in path.read_bytes().split(b"\0") if x]
            if argv and "python" in Path(argv[0]).name:
                processes.append({"pid": int(path.parent.name), "argv": argv})
        except (OSError, UnicodeError):
            pass
    return {"known": True, "error": None, "processes": processes}


def snapshot(config):
    return health.inspect_health(config, inventory=inventory())


def wait_until(predicate, timeout=60, description="test condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.25)
    raise ValueError(f"Timed out waiting for {description}; retain the test case and its logs")


def spawn(case, role):
    if role in health.CADENCES:
        _, config = load_case(case)
        command = [sys.executable, str(case / "probes/probe_universe_quote_watch.py"),
                   "--watchlist-kind", role, "--start-at", config.start_at.isoformat(),
                   "--stop-at", config.stop_at.isoformat(), "--journal-root", str(config.journal.parent),
                   "--journal-schema-version", "2"]
    else:
        command = [sys.executable, str(PACKET / "synthetic_session.py"), role, "--case", str(case)]
    flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
    log = None if os.name == "nt" else (case / f"{role}_console.log").open("w")
    try:
        child = subprocess.Popen(command, cwd=case, creationflags=flags, stdout=log, stderr=log)
    finally:
        if log:
            log.close()
    return child


def prepare(root):
    validate(root)
    inspection = inventory()
    if not inspection["known"]:
        raise ValueError("Native process inspection failed: " + str(inspection["error"]))
    print("SYNTHETIC ONLY. No API, password, production journal, or real poller launch.", flush=True)
    import test_acquisition_health
    result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromModule(test_acquisition_health))
    if not result.wasSuccessful():
        raise ValueError("Offline contract verification failed; test processes were not started")
    marker = PACKET / "LATEST_CASE.json"
    if marker.exists():
        old, config = load_case(latest_case())
        active = [pid for channel in health.CADENCES for pid in health.matching_processes(config, channel, inspection["processes"])]
        if active or process_alive(old.get("monitor_pid")):
            raise ValueError("An earlier synthetic test is still running. Use 05_test_main_stop_all.cmd first")
    from mb_market_data.quote_observation_store import QuoteObservationStore
    from mb_market_data.sampling_membership import SamplingHierarchyRevision
    began = now()
    start = began + timedelta(seconds=12)
    stop = start + timedelta(minutes=30)
    if stop.astimezone(health.ET).date() != start.astimezone(health.ET).date():
        raise ValueError("Run the test at least 31 minutes before midnight ET")
    case = root / "output/polling_health_live_test" / ("case_" + began.strftime("%Y%m%dT%H%M%SZ_") + uuid4().hex[:8])
    (case / "probes").mkdir(parents=True, exist_ok=False)
    (case / "probes/probe_universe_quote_watch.py").write_text(STUB, encoding="utf-8")
    data = {"synthetic": True, "case": str(case), "source_root": str(root), "packet": str(PACKET),
            "start": start.isoformat(), "stop": stop.isoformat(), "native_windows": os.name == "nt"}
    health.atomic_json(case / "case.json", data)
    _, config = load_case(case)
    store = QuoteObservationStore(config.journal, session_date=config.session_date, schema_version=2)
    store.initialize()
    store.record_membership_revision(SamplingHierarchyRevision(session_date=config.session_date,
        revision=0, effective_at=start, uni_symbols=("AAPL", "MSFT", "SPY"),
        focus_symbols=("MSFT",), hot_symbols=(), source="SYNTHETIC real-process acceptance"))
    children = []
    try:
        children.extend([spawn(case, "uni"), spawn(case, "focus")])
        wait_until(lambda: len(health.read_journal(config)["runs"]) == 2, 15, "both synthetic run registrations")
        child = spawn(case, "monitor")
        children.append(child)
        data["monitor_pid"] = child.pid
        data["initial_pids"] = [c.pid for c in children]
        health.atomic_json(case / "case.json", data)
        health.atomic_json(marker, {"case": str(case)})
    except BaseException:
        for channel in (*health.CADENCES, "monitor"):
            (case / f"stop_{channel}.request").touch()
        for child in children:
            try:
                child.wait(timeout=12)
            except subprocess.TimeoutExpired:
                child.terminate()  # only owned, newly spawned SYNTHETIC children
        raise
    print(f"\nOffline checks: PASS ({result.testsRun} tests)")
    print(f"Synthetic case: {case}\nSynthetic journal: {config.journal}")
    print("Opened test-poll-uni, test-poll-focus, and test-health.")
    print("Wait up to 45 seconds for UNI and FOCUS to show RUNNING in test-health.")
    print("Next: call 02_test_main_fail_focus.cmd in prod-main. No password prompts.")
    print("Maximum run time: 30 minutes. Finish early with 04_test_main_finish.cmd.")
    return case


def poller_main(case):
    _, config = load_case(case)
    parser = argparse.ArgumentParser()
    parser.add_argument("--watchlist-kind", choices=tuple(health.CADENCES), required=True)
    parser.add_argument("--start-at", required=True)
    parser.add_argument("--stop-at", required=True)
    parser.add_argument("--journal-root", type=Path, required=True)
    parser.add_argument("--journal-schema-version", type=int, required=True)
    args = parser.parse_args()
    if (health.timestamp(args.start_at) != config.start_at or health.timestamp(args.stop_at) != config.stop_at
            or args.journal_root.resolve() != config.journal.parent or args.journal_schema_version != 2):
        raise ValueError("Synthetic launcher arguments do not match its isolated case")
    from mb_market_data.quote_observation_store import PollRunProvenance, QuoteObservationStore
    from mb_market_data.schwab_quotes import QuoteBatchResult, QuoteResult, QuoteStatus
    from mb_market_data.watchlist_polling import PollRequest, PollSlot, WatchlistKind
    channel = args.watchlist_kind
    title("test-poll-" + channel)
    run = "SYNTHETIC:" + channel + ":" + uuid4().hex
    evidence = case / "evidence" / run.replace(":", "_")
    evidence.mkdir(parents=True)
    manifest = {"journal_run_id": run, "journal_schema_version": 2, "journal_errors": 0,
                "completed_at_et": None, "synthetic": True}
    health.atomic_json(evidence / "manifest.json", manifest)
    (evidence / "errors.log").write_text("")
    store = QuoteObservationStore(config.journal, session_date=config.session_date, schema_version=2)
    store.record_run(PollRunProvenance(run_id=run, started_at=now(), software_version="SYNTHETIC",
        configuration={"watchlist_kind": channel, "poll_window_start_et": config.start_at.isoformat(),
            "poll_window_end_et": config.stop_at.isoformat(), "evidence_run_directory": str(evidence)}))
    print(f"SYNTHETIC {channel.upper()} poller. Fake quotes only; no API or password.\n{config.journal}", flush=True)
    launched_at = now()
    slots = [slot for slot in health.configured_slots(config, channel) if slot >= launched_at]
    sequence = 0
    try:
        for slot in slots:
            while now() < slot:
                if (case / f"stop_{channel}.request").exists():
                    print("Synthetic stop request received.", flush=True)
                    return 0
                fault = case / "fail_focus.request"
                if (channel == "focus" and fault.exists()
                        and json.loads(fault.read_text())["run_id"] == run):
                    manifest["journal_errors"] = 1
                    (evidence / "errors.log").write_text("SYNTHETIC intentional Focus failure. No real database lock was injected.\n")
                    print("INTENTIONAL SYNTHETIC FAILURE. Uni continues. Use the synthetic restart launcher.", flush=True)
                    return 7
                time.sleep(.1)
            symbols = ("AAPL", "MSFT", "SPY") if channel == "uni" else ("MSFT",)
            began = now()
            request = PollRequest(PollSlot(WatchlistKind(channel), slot), 0, symbols, began)
            results = tuple(QuoteResult(symbol, QuoteStatus.QUOTE, {"symbol": symbol,
                "quote": {"lastPrice": 100 + sequence / 100, "totalVolume": 1000 + sequence,
                          "tradeTime": int(began.timestamp() * 1000), "quoteTime": int(began.timestamp() * 1000)}},
                None, 1, began, now()) for symbol in symbols)
            store.record_acquisition(run, request, QuoteBatchResult(results, 1, 400, ()), completed_at=now())
            sequence += 1
            print(f"SYNTHETIC sample {sequence}: {channel} {slot.astimezone(health.ET).strftime('%H:%M:%S ET')} COMMITTED", flush=True)
        return 0
    finally:
        manifest["completed_at_et"] = now().astimezone(health.ET).isoformat()
        health.atomic_json(evidence / "manifest.json", manifest)


def monitor_case(case):
    _, config = load_case(case)
    title("test-health")
    if os.name != "nt":
        health.windows_process_inventory = inventory
    print("SYNTHETIC SESSION. This window observes only the separate test journal.", flush=True)
    return cli.monitor(config, interval=2, stop_file=case / "stop_monitor.request",
                       poller_windows="test-poll-uni / test-poll-focus")


def monitor_snapshot(case, config):
    path = case / "output/polling_health" / str(config.session_date) / "latest.json"
    return json.loads(path.read_text()) if path.exists() else None


def fail_focus(case):
    _, config = load_case(case)
    if (case / "failed_phase.json").exists():
        raise ValueError("This test already has a recorded failure; continue with 03 or prepare a new case")
    def healthy():
        value = monitor_snapshot(case, config)
        return value if value and value["journal_read"] == "OK" and set(value["channels"]) == set(health.CADENCES) and all(info["state"] == "RUNNING" and info["latest_run_committed_slots"] > 0
            for info in value["channels"].values()) else None
    print("Waiting for both synthetic channels to commit their first samples...", flush=True)
    before = wait_until(healthy, 65, "healthy synthetic pollers")
    health.atomic_json(case / "healthy_phase.json", before)
    print("Requesting an intentional SYNTHETIC Focus failure. Uni is left running.", flush=True)
    health.atomic_json(case / "fail_focus.request", {"run_id": before["channels"]["focus"]["latest_run_id"],
                                                    "requested_at_utc": now().isoformat()})
    def failed():
        value = monitor_snapshot(case, config)
        if (value and value["channels"]["focus"]["state"] == "FAILED"
                and value["channels"]["focus"]["missing_slot_count"] > 0
                and value["channels"]["uni"]["state"] == "RUNNING"
                and value["channels"]["uni"]["committed_acquisitions"] > before["channels"]["uni"]["committed_acquisitions"]):
            return value
    print("Waiting up to 50 seconds to witness a missed Focus slot and another Uni commit...", flush=True)
    after = wait_until(failed, 55, "Focus alert, visible gap, and uninterrupted Uni")
    health.atomic_json(case / "failed_phase.json", after)
    print(health.format_health(after), flush=True)
    print("FAILURE CHECK: PASS. The monitor should have shown a popup and sounded a beep.")
    print("Next: call 03_test_main_restart_focus.cmd. It restarts only this synthetic Focus poller.")


def restart_case(case):
    _, config = load_case(case)
    if not (case / "failed_phase.json").exists():
        raise ValueError("Complete the synthetic failure check first")
    if (case / "recovered_phase.json").exists():
        raise ValueError("Recovery is already recorded for this case")
    before = snapshot(config)
    health.restart_command(config, "focus", before, inventory())
    # Preserve the fault receipt. It targets the old run exclusively, so the
    # replacement cannot inherit it even if a file view is briefly stale.
    child = spawn(case, "restart-worker")
    health.atomic_json(case / "restart_worker.json", {"pid": child.pid})
    failed = json.loads((case / "failed_phase.json").read_text())
    def recovered():
        value = monitor_snapshot(case, config)
        if (value and value["channels"]["focus"]["state"] == "RUNNING"
                and value["channels"]["focus"]["latest_run_id"] != failed["channels"]["focus"]["latest_run_id"]
                and value["channels"]["focus"]["latest_run_committed_slots"] > 0
                and value["channels"]["focus"]["missing_slot_count"] >= failed["channels"]["focus"]["missing_slot_count"]):
            return value
        if child.poll() is not None:
            raise ValueError(f"Synthetic guided restart exited ({child.returncode}); inspect its receipt")
    print("Waiting for the restarted synthetic Focus run to commit a NEW sample...", flush=True)
    after = wait_until(recovered, 45, "durable Focus recovery")
    health.atomic_json(case / "recovered_phase.json", after)
    print(health.format_health(after), flush=True)
    print("RESTART CHECK: PASS. New run committed to the SAME synthetic journal; earlier gaps remain.")
    print("Next: press Esc, then Ctrl+C in test-health. Leave the synthetic pollers running.")
    print("Then call 04_test_main_finish.cmd in prod-main. If Ctrl+C fails, try Q in test-health.")


def restart_worker(case):
    _, config = load_case(case)
    title("test-poll-focus")
    if os.name != "nt":
        health.windows_process_inventory = inventory
    return cli.restart(config, "focus", launch_notice="SYNTHETIC poller only. No API or password prompt.")


def process_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: PID no longer exists
                return False
            raise ValueError("Cannot establish whether the synthetic monitor has exited")
        try:
            status = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(status)):
                raise ValueError("Cannot read synthetic monitor exit status")
            return status.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        # An unreaped Linux test child is exited despite retaining a /proc entry.
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
    except FileNotFoundError:
        return False


def request_stop(case):
    _, config = load_case(case)
    for role in (*health.CADENCES, "monitor"):
        (case / f"stop_{role}.request").touch()
    print("Stop requests sent only to this synthetic case. Waiting for its processes to exit...", flush=True)
    def stopped():
        data = json.loads((case / "case.json").read_text())
        inspect = inventory()
        if not inspect["known"]:
            raise ValueError("Native process inspection unavailable during cleanup")
        return not process_alive(data.get("monitor_pid")) and not any(
            health.matching_processes(config, channel, inspect["processes"]) for channel in health.CADENCES)
    wait_until(stopped, 20, "synthetic process shutdown")
    worker_file = case / "restart_worker.json"
    if worker_file.exists():
        worker_pid = json.loads(worker_file.read_text())["pid"]
        wait_until(lambda: not process_alive(worker_pid), 15, "guided restart receipt completion")
    print("Synthetic processes stopped. Production polling and journals were unchanged.", flush=True)


def finish(case, *, observations=None):
    data, config = load_case(case)
    if process_alive(data.get("monitor_pid")):
        raise ValueError("test-health is still running. Try Esc then Ctrl+C, or Q. Use 05 to stop all test processes")
    stopped_path = case / "output/polling_health" / str(config.session_date) / "last_stop.json"
    stopped = json.loads(stopped_path.read_text()) if stopped_path.exists() else {}
    if stopped.get("monitor_pid") != data["monitor_pid"] or stopped.get("stopped_by") not in {"keyboard interrupt", "Q key", "stop request"}:
        raise ValueError("Clean monitor shutdown was not recorded. Preserve this case for investigation; use 05 for cleanup")
    if not (case / "recovered_phase.json").exists():
        raise ValueError("Complete failure and restart checks before finishing; use 05 for cleanup")
    final = snapshot(config)
    if any(info["state"] != "RUNNING" or len(info["candidate_pids"]) != 1
           for info in final["channels"].values()) or set(final["channels"]) != set(health.CADENCES):
        raise ValueError("Both synthetic pollers must still be running after monitor shutdown; use 05 for cleanup")
    request_stop(case)
    incidents = json.loads((stopped_path.parent / "incidents.json").read_text())
    resolved = [x for x in incidents if ":FAILED:" in x["key"] and x["resolved_at_utc"]]
    unresolved_gaps = [x for x in incidents if x["key"] == "focus:coverage" and not x["resolved_at_utc"]]
    receipts = list((case / "output/polling_restart_requests").rglob("request.json"))
    records = [json.loads(path.read_text()) for path in receipts]
    if not resolved or not unresolved_gaps or len(records) != 1 or records[0].get("exit_code") != 0:
        raise ValueError("Incident history or guided restart receipt differs from expectations; retain the case")
    if observations is None:
        observations = {"popup_seen": input("Did the intentional-failure popup appear? (y/n): ").strip().lower() == "y",
                        "beep_heard": input("Did you hear the alert beep? (y/n): ").strip().lower() == "y"}
    report = {"synthetic": True, "native_windows": data["native_windows"], "case": str(case),
        "journal": str(config.journal), "completed_at_utc": now().isoformat(),
        "failure_restart_checks": "PASS", "monitor_stop": stopped["stopped_by"],
        "ctrl_c_confirmed": stopped["stopped_by"] == "keyboard interrupt", "observations": observations,
        "pollers_running_after_monitor_exit": True,
        "focus_missing_slots_preserved": final["channels"]["focus"]["missing_slot_count"],
        "failure_incidents_resolved": len(resolved), "guided_restart_receipt": str(receipts[0]),
        "final_health": final}
    health.atomic_json(case / "acceptance.json", report)
    print("REAL-PROCESS SYNTHETIC FAILURE / RESTART: PASS")
    print("Clean monitor stop: " + stopped["stopped_by"])
    print("Ctrl+C: " + ("CONFIRMED" if report["ctrl_c_confirmed"] else "NOT CONFIRMED"))
    print(f"Earlier missed Focus slots preserved: {report['focus_missing_slots_preserved']}")
    print("Popup: " + ("CONFIRMED" if observations["popup_seen"] else "NOT CONFIRMED"))
    print("Beep: " + ("CONFIRMED" if observations["beep_heard"] else "NOT CONFIRMED"))
    print(f"Acceptance report: {case / 'acceptance.json'}")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "fail", "restart", "restart-worker", "monitor", "finish", "stop"))
    parser.add_argument("--market-data-root", type=Path)
    parser.add_argument("--case", type=Path)
    args = parser.parse_args()
    try:
        if args.operation == "prepare":
            if args.market_data_root is None:
                raise ValueError("Preparation requires --market-data-root")
            prepare(args.market_data_root.resolve())
            return 0
        case = args.case or latest_case()
        functions = {"fail": fail_focus, "restart": restart_case, "restart-worker": restart_worker,
                     "monitor": monitor_case, "finish": finish, "stop": request_stop}
        result = functions[args.operation](case)
        return result if isinstance(result, int) else 0
    except KeyboardInterrupt:
        print("Test control interrupted. Use 05_test_main_stop_all.cmd to stop this synthetic case.", flush=True)
        return 130
    except (OSError, ValueError, KeyError) as error:
        print(f"Synthetic test STOPPED: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
