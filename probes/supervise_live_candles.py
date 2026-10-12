"""Supervise one bounded candle producer with quiet warnings and a watchdog."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, timezone

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from mb_market_data.candle_capture import bounds, synthetic_bounds, validate_window
from mb_market_data.candle_receipts import CandleReceiptReader, CandleReceiptError
from mb_market_data.candle_supervision import (
    CandleHealth, DirectoryLock, HEARTBEAT_GRACE, health_view, instant, publish_json, stamp,
)

UTC = timezone.utc


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def producer_phase(folder, fallback):
    try:
        value = load(folder / "producer_status.json")
        if not isinstance(value, dict) or value.get("state") not in {
                "STARTING", "WAITING_FOR_CREDENTIALS", "RUNNING", "DURATION_COMPLETE",
                "STOPPED_BY_OPERATOR", "WINDOW_ENDED", "STOPPED_AFTER_ERRORS", "FAILED"}:
            raise ValueError("Malformed producer status")
        instant(value["started_at_utc"])
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return {"state": "FAILED", "started_at_utc": fallback}


def new_session(args):
    now = datetime.now(UTC)
    from zoneinfo import ZoneInfo
    session_date = now.astimezone(ZoneInfo("America/New_York")).date()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", args.symbol):
        raise ValueError("Invalid candle symbol")
    if args.synthetic_scenario is None:
        validate_window(session_date, now)
        if not args.market_open_confirmed:
            raise ValueError("Use --market-open-confirmed only during a known open regular session")
        if args.symbol != "SPY" or args.duration != 720 or args.interval != 15:
            raise ValueError("The initial live trial is SPY, 720 seconds, 15-second requests")
        if (bounds(session_date)[1] - now).total_seconds() < args.duration + 15:
            raise ValueError("Allow at least 13 minutes before the regular-session end")
    elif not (0 < args.duration <= 30 and .1 <= args.interval <= 2):
        raise ValueError("Synthetic drills require duration <=30s and interval .1-2s")
    root = args.output_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    folder = root / (now.strftime("%Y-%m-%d-%H-%M-%S") + "-" + args.symbol + "-" + uuid.uuid4().hex[:8])
    folder.mkdir()
    plan = {"schema_version": 1, "session_id": uuid.uuid4().hex,
            "session_date": str(session_date), "symbol": args.symbol,
            "created_at_utc": stamp(now), "interval_seconds": args.interval,
            "duration_seconds": args.duration, "http_timeout_seconds": 10,
            "simulation": args.synthetic_scenario is not None,
            "journal_access": "NONE", "quote_pollers_owned": False,
            "audible_alarm": False}
    publish_json(folder / "session_plan.json", plan)
    (folder / "commands").mkdir()
    print(f"CANDLE SESSION: {folder}", flush=True)
    print("Quiet warnings only. Quote-polling alarms and production processes are independent.", flush=True)
    return folder


def start_run(root, plan, scenario=None):
    number = len(list(root.glob("run_*"))) + 1
    folder = root / f"run_{number:04d}"
    folder.mkdir()
    session_date = date.fromisoformat(plan["session_date"])
    start, end = synthetic_bounds(session_date) if plan["simulation"] else bounds(session_date)
    now = stamp()
    publish_json(folder / "request_plan.json", {
        "symbol": plan["symbol"], "session_date": plan["session_date"],
        "session_id": plan["session_id"], "run_id": folder.name,
        "started_at_utc": now, "request_start_et": start.isoformat(), "request_end_et": end.isoformat(),
        "frequency_minutes": 5, "extended_hours": False,
        "duration_seconds": plan["duration_seconds"], "interval_seconds": plan["interval_seconds"],
        "simulation": plan["simulation"], "journal_access": "NONE"})
    publish_json(folder / "producer_status.json", {"state": "STARTING", "started_at_utc": now})
    command = [sys.executable, "-u", str(PROJECT_ROOT / "probes" / "capture_supervised_candles.py"),
               "--symbol", plan["symbol"], "--session-date", plan["session_date"],
               "--duration-seconds", str(plan["duration_seconds"]),
               "--interval-seconds", str(plan["interval_seconds"]), "--run-directory", str(folder)]
    if plan["simulation"]:
        command += ["--synthetic-scenario", scenario or "healthy"]
    # Inherit console input and output. The supervisor never reads credentials.
    child = subprocess.Popen(command)
    publish_json(folder / "launch_receipt.json", {"pid": child.pid, "session_id": plan["session_id"],
                 "run_id": folder.name, "launched_at_utc": stamp(), "owned_child": True})
    return folder, child, CandleReceiptReader(folder)


def watchdog(root):
    plan = load(root / "session_plan.json")
    with DirectoryLock(root / "watchdog.lock"):
        while not (root / "close-request.json").exists():
            now = datetime.now(UTC)
            state, message = "WATCHING", "Independent candle supervisor heartbeat check"
            try:
                value = load(root / "supervisor_status.json")
                age = (now - instant(value["heartbeat_at_utc"])).total_seconds()
                if value["session_id"] != plan["session_id"] or age < -2 or age > HEARTBEAT_GRACE:
                    state, message = "WARNING", "Candle supervisor heartbeat is stale"
            except (OSError, ValueError, KeyError, TypeError):
                state, message = "WARNING", "Candle supervisor status is unavailable"
            try:
                publish_json(root / "watchdog_status.json", {"session_id": plan["session_id"],
                             "state": state, "message": message, "heartbeat_at_utc": stamp(now),
                             "pid": os.getpid(), "audible_alarm": False}, replace=True)
            except OSError as exc:
                print(f"CANDLE watchdog status warning: {type(exc).__name__}; still watching", flush=True)
            time.sleep(.5)
    return 0


def stop_owned(child, folder, touch):
    if child.poll() is not None:
        return
    if not (folder / "stop-request.json").exists():
        publish_json(folder / "stop-request.json", {"requested_at_utc": stamp()})
    deadline = time.monotonic() + 12
    while child.poll() is None and time.monotonic() < deadline:
        touch()
        time.sleep(.1)
    forced = child.poll() is None
    if forced:
        # This Popen handle identifies only the child this supervisor launched.
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)
    publish_json(folder / "stop_receipt.json", {"owned_pid": child.pid, "stopped_at_utc": stamp(),
                 "forced_owned_child_cleanup": forced, "returncode": child.returncode})
    publish_json(folder / "producer_status.json", {"state": "STOPPED_BY_OPERATOR", "started_at_utc": stamp(),
                 "updated_at_utc": stamp(), "forced_owned_child_cleanup": forced}, replace=True)


def supervise(root, scenario=None):
    root = root.resolve(strict=True)
    plan = load(root / "session_plan.json")
    with DirectoryLock(root / "supervisor.lock"):
        monitor = CandleHealth(root, plan)
        folder, child, reader = start_run(root, plan, scenario)
        try:
            watcher = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()), "watchdog", "--session", str(root)])
        except OSError:
            stop_owned(child, folder, lambda: None)
            raise
        seen_commands, previous, last_value = set(), None, None
        def touch():
            if last_value:
                publish_json(root / "supervisor_status.json", {**last_value, "heartbeat_at_utc": stamp()}, replace=True)
        try:
            while True:
                reader_error = None
                try:
                    reader.refresh(limit=32)
                except (OSError, ValueError, TypeError, CandleReceiptError) as exc:
                    reader_error = type(exc).__name__
                phase = producer_phase(folder, plan["created_at_utc"])
                if not (folder / "stop_receipt.json").exists() and (root / "close-request.json").exists():
                    stop_owned(child, folder, touch)
                    phase = load(folder / "producer_status.json")
                last_value = monitor.inspect(run_id=folder.name, phase=phase, alive=child.poll() is None,
                                             reader=reader, now=datetime.now(UTC), reader_error=reader_error)
                last_value.update(supervisor_pid=os.getpid(), producer_pid=child.pid, producer_returncode=child.poll())
                if watcher.poll() is not None and not (root / "close-request.json").exists():
                    last_value.update(state="WARNING", severity="warning",
                                      message="Independent candle watchdog exited; acquisition remains separate")
                if (root / "close-request.json").exists():
                    last_value.update(state="SESSION_CLOSED", severity="notice", message="Candle session closed; evidence retained")
                try:
                    publish_json(root / "supervisor_status.json", last_value, replace=True)
                except OSError as exc:
                    print(f"CANDLE status warning: {type(exc).__name__}; supervision continues", flush=True)
                line = last_value["state"], last_value["message"]
                if line != previous:
                    print(f"CANDLE {plan['symbol']}: {line[0]} — {line[1]}", flush=True)
                    previous = line
                if (root / "close-request.json").exists():
                    break
                for command_path in sorted((root / "commands").glob("*.json")):
                    if command_path.name in seen_commands:
                        continue
                    command = load(command_path)
                    seen_commands.add(command_path.name)
                    outcome = "REFUSED"
                    if command.get("session_id") == plan["session_id"] and command.get("expected_run") == folder.name:
                        if command["action"] == "stop":
                            stop_owned(child, folder, touch)
                            outcome = "STOPPED"
                        elif command["action"] == "restart" and child.poll() is not None:
                            if not plan["simulation"]:
                                validate_window(date.fromisoformat(plan["session_date"]), datetime.now(UTC))
                            folder, child, reader = start_run(root, plan, command.get("synthetic_scenario"))
                            outcome = "RESTARTED_AWAITING_RESPONSE"
                    publish_json(root / ("command_receipt_" + command_path.name), {
                        "request": command, "result": outcome, "completed_at_utc": stamp(), "active_run": folder.name})
                time.sleep(.2)
        finally:
            if not (root / "close-request.json").exists():
                publish_json(root / "close-request.json", {"session_id": plan["session_id"], "requested_at_utc": stamp()})
            try:
                stop_owned(child, folder, touch)
            finally:
                try:
                    watcher.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    watcher.terminate()
                    watcher.wait(timeout=3)
    return 0


def request(root, action, scenario=None):
    root = root.resolve(strict=True)
    plan = load(root / "session_plan.json")
    if action == "close":
        # Works even after supervisor failure. The watchdog and producer read
        # the stop files; it does not enumerate or kill unrelated processes.
        for folder in root.glob("run_*"):
            if not (folder / "stop-request.json").exists():
                publish_json(folder / "stop-request.json", {"requested_at_utc": stamp()})
        if not (root / "close-request.json").exists():
            publish_json(root / "close-request.json", {"session_id": plan["session_id"], "requested_at_utc": stamp()})
        print("Candle session close requested", flush=True)
        return
    value = health_view(root)
    if "active_run" not in value or "supervisor" in value.get("message", "").lower():
        raise ValueError("A current candle supervisor is required for this request")
    if scenario and not plan["simulation"]:
        raise ValueError("Synthetic scenarios cannot be used in a live session")
    publish_json(root / "commands" / (uuid.uuid4().hex + ".json"), {
        "session_id": plan["session_id"], "expected_run": value["active_run"],
        "action": action, "requested_at_utc": stamp(), "synthetic_scenario": scenario})
    print(f"Candle {action} requested; inspect the command receipt and health status", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "inspect", "stop", "restart", "close", "watchdog"))
    parser.add_argument("--session", type=Path)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--duration", type=float, default=720)
    parser.add_argument("--interval", type=float, default=15)
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT / "output" / "supervised_candles")
    parser.add_argument("--synthetic-scenario", choices=("healthy", "api-error", "hang", "exit"))
    parser.add_argument("--market-open-confirmed", action="store_true")
    args = parser.parse_args()
    args.symbol = args.symbol.strip().upper()
    if args.action != "start" and args.session is None:
        parser.error("--session is required")
    try:
        if args.action == "start":
            if args.session is not None:
                raise ValueError("Start always creates a fresh evidence session")
            return supervise(new_session(args), args.synthetic_scenario)
        if args.action == "watchdog":
            return watchdog(args.session)
        if args.action == "inspect":
            print(json.dumps(health_view(args.session), indent=2))
        else:
            request(args.session, args.action, args.synthetic_scenario)
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"CANDLE supervision stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
