"""Observe current polling or manually restart one stopped channel.

The packet copy imports its adjacent, isolated health module. The repository
copy imports the same module from mb_market_data. Neither changes the poller.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from datetime import date, datetime, time as clock_time
from pathlib import Path
from uuid import uuid4

try:
    import acquisition_health as health
except ModuleNotFoundError:
    from mb_market_data import acquisition_health as health


def config_for(root: Path, session: date, journal: Path | None = None, *, grace: float = 90) -> health.HealthConfig:
    root = root.resolve()
    journal = journal or root / f"output/quote_observation_journal_v2_api_opening_{session}/{session}.sqlite3"
    return health.HealthConfig(root, journal.resolve(), session,
                              datetime.combine(session, clock_time(9, 30), tzinfo=health.ET),
                              datetime.combine(session, clock_time(16), tzinfo=health.ET), grace)


def popup_worker(messages: queue.Queue) -> None:
    while True:
        message = messages.get()
        try:
            import ctypes
            import winsound
            winsound.MessageBeep(winsound.MB_ICONHAND)
            ctypes.windll.user32.MessageBoxW(None, message, "Polling health alert", 0x10 | 0x10000)
        except Exception:
            print("AUDIBLE/POPUP ALERT UNAVAILABLE", flush=True)
        finally:
            messages.task_done()


def wait_for_stop(interval: float, stop_file: Path | None = None) -> str | None:
    """Keep the Windows keyboard and explicit stop request responsive."""
    deadline = time.monotonic() + interval
    while True:
        if stop_file is not None and stop_file.exists():
            return "stop request"
        if os.name == "nt":
            import msvcrt
            while msvcrt.kbhit():
                key = msvcrt.getwch()
                if key in ("\x00", "\xe0"):
                    msvcrt.getwch()  # discard extended-key scan code (PgDn is Q)
                    continue
                if key.lower() == "q":
                    return "Q key"
                if key == "\x03":
                    raise KeyboardInterrupt
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(0.1, remaining))


def monitor(config: health.HealthConfig, *, once: bool = False, interval: float = 5,
            popups: bool = True, stop_file: Path | None = None,
            poller_windows: str = "prod-poll-uni / prod-poll-focus") -> int:
    directory = config.market_data_root / "output/polling_health" / str(config.session_date)
    clear_old_stop = stop_file is None
    stop_file = stop_file or directory / "stop.request"
    messages: queue.Queue = queue.Queue(maxsize=1)
    if os.name == "nt" and popups:
        threading.Thread(target=popup_worker, args=(messages,), daemon=True).start()
    previous = None
    reminder_at = 0.0
    print("Independent read-only polling monitor. No password or API requests.")
    print(f"Status and persistent alerts: {directory}", flush=True)
    print("Journal read failures are reported and retried. This monitor never restarts a poller automatically.", flush=True)
    print("Stop this monitor with Ctrl+C or Q. Clear any CMD text selection with Esc first.", flush=True)
    handlers = {}
    if threading.current_thread() is threading.main_thread():
        for name in ("SIGINT", "SIGBREAK"):
            if hasattr(signal, name):
                event = getattr(signal, name)
                handlers[event] = signal.getsignal(event)
                signal.signal(event, signal.default_int_handler)
    stopped_by = "error"
    try:
        with health.operation_lock("monitor:" + str(config.journal).casefold()):
            if clear_old_stop:
                stop_file.unlink(missing_ok=True)
            while True:
                reason = wait_for_stop(0, stop_file)
                if reason:
                    stopped_by = reason
                    print(f"Monitor stopped by {reason}. Pollers and journal unchanged.", flush=True)
                    return 0
                snapshot = health.inspect_health(config)
                new = health.persist_health(directory, snapshot)
                signature = json.dumps({"read": snapshot["journal_read"], "error": snapshot["journal_error"],
                                        "channels": snapshot["channels"], "process": snapshot["process_inspection"]}, sort_keys=True)
                if signature != previous:
                    print(health.format_health(snapshot), flush=True)
                    previous = signature
                if new:
                    detected = health.timestamp(snapshot["observed_at_utc"]).astimezone(health.ET).strftime("%H:%M:%S ET")
                    message = f"Detected at {detected}. Check STATUS.txt for current status.\n\n"
                    message += "\n".join(f"{item['key']}: {item['detail']}" for item in new)
                    message += f"\n\nCheck {poller_windows}. Keep healthy pollers running.\nDetails: {directory / 'STATUS.txt'}"
                    print("PERSISTENT ALERT:\n" + message, flush=True)
                    if os.name == "nt" and popups and messages.empty():
                        messages.put_nowait(message[-6000:])
                if health.alert_conditions(snapshot) and time.monotonic() >= reminder_at:
                    if os.name == "nt" and popups:
                        import winsound
                        winsound.MessageBeep(winsound.MB_ICONHAND)
                    else:
                        print("\a", end="", flush=True)
                    reminder_at = time.monotonic() + 60
                if once:
                    stopped_by = "one inspection"
                    return 0 if not health.alert_conditions(snapshot) else 1
                reason = wait_for_stop(interval, stop_file)
                if reason:
                    stopped_by = reason
                    print(f"Monitor stopped by {reason}. Pollers and journal unchanged.", flush=True)
                    return 0
    except KeyboardInterrupt:
        stopped_by = "keyboard interrupt"
        print("\nMonitor stopped by keyboard interrupt. Pollers and journal unchanged.", flush=True)
        return 130
    finally:
        for event, handler in handlers.items():
            signal.signal(event, handler)
        health.atomic_json(directory / "last_stop.json", {
            "monitor_pid": os.getpid(), "stopped_at_utc": datetime.now(health.UTC).isoformat(),
            "stopped_by": stopped_by, "journal": str(config.journal)})


def restart(config: health.HealthConfig, channel: str, *, plan_only: bool = False,
            launch_notice: str = "The existing poller will prompt for your encrypted-config password.") -> int:
    # The OS mutex is held for the replacement child's whole lifetime. A
    # second guided restart cannot race the first during its password prompt.
    with health.operation_lock("restart:" + str(config.journal).casefold() + ":" + channel):
        inventory = health.windows_process_inventory()
        snapshot = health.inspect_health(config, inventory=inventory)
        command = health.restart_command(config, channel, snapshot, inventory)
        print(health.format_health(snapshot), flush=True)
        print("Manual restart command: " + subprocess.list2cmdline(command), flush=True)
        print(launch_notice, flush=True)
        print("This restores future slots in the original journal. It does not salvage old samples.", flush=True)
        if plan_only:
            print("Plan only: no poller launched.", flush=True)
            return 0
        # Inspect once more immediately before launch under the operation lock.
        inventory = health.windows_process_inventory()
        snapshot = health.inspect_health(config, inventory=inventory)
        command = health.restart_command(config, channel, snapshot, inventory)
        health.check_write_available(config)
        case = config.market_data_root / "output/polling_restart_requests" / str(config.session_date) / (
            datetime.now(health.UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + channel + "_" + uuid4().hex[:8])
        receipt = {"channel": channel, "journal": str(config.journal), "command": command,
                   "requested_at_utc": datetime.now(health.UTC).isoformat(), "health_before": snapshot,
                   "exit_code": None, "launch_error": None}
        health.atomic_json(case / "request.json", receipt)
        print("Launching one replacement poller. Monitor confirmation requires a NEW COMMITTED SLOT.", flush=True)
        try:
            result = subprocess.run(command, cwd=config.market_data_root, check=False)
            receipt["exit_code"] = result.returncode
            return result.returncode
        except OSError as error:
            receipt["launch_error"] = f"{type(error).__name__}: {error}"
            raise
        finally:
            receipt["child_finished_at_utc"] = datetime.now(health.UTC).isoformat()
            health.atomic_json(case / "request.json", receipt)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("monitor", "restart", "windows-check", "stop-monitor"))
    parser.add_argument("--market-data-root", type=Path, required=True)
    parser.add_argument("--session-date", type=date.fromisoformat)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--channel", choices=tuple(health.CADENCES))
    parser.add_argument("--grace-seconds", type=float, default=90)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-popup", action="store_true")
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.operation == "windows-check":
            inventory = health.windows_process_inventory()
            if not inventory["known"]:
                raise ValueError(inventory["error"])
            with health.operation_lock("windows-acceptance-check"):
                pass
            print(f"Windows process inspection and operation mutex: PASS ({len(inventory['processes'])} Python processes inspected)")
            print("No production journal was opened. No poller was started or stopped.")
            return 0
        session = args.session_date
        if session is None:
            default = datetime.now(health.ET).date()
            value = input(f"Session date (YYYY-MM-DD) [{default}]: ").strip()
            session = date.fromisoformat(value) if value else default
        config = config_for(args.market_data_root, session, args.journal, grace=args.grace_seconds)
        if args.operation == "stop-monitor":
            stop_file = args.stop_file or config.market_data_root / "output/polling_health" / str(session) / "stop.request"
            health.atomic_json(stop_file, {"requested_at_utc": datetime.now(health.UTC).isoformat()})
            print(f"Monitor stop requested for {session}. Pollers and journal unchanged.", flush=True)
            return 0
        if args.interval < 1:
            raise ValueError("Monitor interval must be at least one second")
        if args.operation == "restart":
            if args.channel is None:
                raise ValueError("Manual restart requires --channel")
            return restart(config, args.channel, plan_only=args.plan_only)
        return monitor(config, once=args.once, interval=args.interval, popups=not args.no_popup,
                       stop_file=args.stop_file)
    except KeyboardInterrupt:
        print("\nStopped by operator. Check the affected poller window if a restart was in progress.")
        return 130
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Polling health tool STOPPED: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
