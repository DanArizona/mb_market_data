"""Observe current polling or manually restart one stopped channel.

The packet copy imports its adjacent, isolated health module. The repository
copy imports the same module from mb_market_data. Neither changes the poller.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import date, datetime, time as clock_time
from pathlib import Path
from uuid import uuid4

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if SOURCE_ROOT.is_dir() and str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

try:
    import acquisition_health as health
except ModuleNotFoundError:
    from mb_market_data import acquisition_health as health

try:
    import polling_alarm as alarm
except ModuleNotFoundError:
    from mb_market_data import polling_alarm as alarm


def config_for(root: Path, session: date, journal: Path | None = None, *, grace: float = 90) -> health.HealthConfig:
    root = root.resolve()
    journal = journal or root / f"output/quote_observation_journal_v2_api_opening_{session}/{session}.sqlite3"
    return health.HealthConfig(root, journal.resolve(), session,
                              datetime.combine(session, clock_time(9, 30), tzinfo=health.ET),
                              datetime.combine(session, clock_time(16), tzinfo=health.ET), grace)


def wait_for_stop(interval: float, stop_file: Path | None = None, *, keyboard: bool = True) -> str | None:
    """Keep the Windows keyboard and explicit stop request responsive."""
    deadline = time.monotonic() + interval
    while True:
        if stop_file is not None and stop_file.exists():
            return "stop request"
        if os.name == "nt" and keyboard:
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
            poller_windows: str = "prod-poll-uni / prod-poll-focus",
            keyboard: bool = True, monitor_token: str | None = None) -> int:
    directory = config.market_data_root / "output/polling_health" / str(config.session_date)
    clear_old_stop = stop_file is None
    stop_file = stop_file or directory / "stop.request"
    desktop = None
    previous = None
    print("Independent read-only polling monitor. No password or API requests.")
    print(f"Status and persistent alerts: {directory}", flush=True)
    print("Journal read failures are reported and retried. This monitor never restarts a poller automatically.", flush=True)
    print("Stop this monitor with Ctrl+C or Q. Clear any CMD text selection with Esc first.", flush=True)
    if os.name == "nt" and popups:
        print("Unresolved failures: persistent topmost window and three tones every 10 seconds.", flush=True)
        print("Silence for 5 minutes keeps monitoring active. New failures sound immediately.", flush=True)
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
            if os.name == "nt" and popups:
                desktop = alarm.DesktopAlarm()
                desktop.start()
            if clear_old_stop:
                stop_file.unlink(missing_ok=True)
            while True:
                reason = wait_for_stop(0, stop_file, keyboard=keyboard)
                if reason:
                    stopped_by = reason
                    print(f"Monitor stopped by {reason}. Pollers and journal unchanged.", flush=True)
                    return 0
                snapshot = health.inspect_health(config)
                snapshot.update(monitor_pid=os.getpid(), monitor_token=monitor_token)
                new = health.persist_health(directory, snapshot)
                if desktop:
                    incidents = json.loads((directory / "incidents.json").read_text(encoding="utf-8"))
                    desktop.publish(alarm.alarm_payload(
                        snapshot, health.alert_conditions(snapshot), incidents,
                        window_active=health.timestamp(config.start_at) <= health.timestamp(snapshot["observed_at_utc"]) < health.timestamp(config.stop_at),
                        status_path=directory / "STATUS.txt", poller_windows=poller_windows))
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
                if once:
                    stopped_by = "one inspection"
                    return 0 if not health.alert_conditions(snapshot) else 1
                reason = wait_for_stop(interval, stop_file, keyboard=keyboard)
                if reason:
                    stopped_by = reason
                    print(f"Monitor stopped by {reason}. Pollers and journal unchanged.", flush=True)
                    return 0
    except KeyboardInterrupt:
        stopped_by = "keyboard interrupt"
        print("\nMonitor stopped by keyboard interrupt. Pollers and journal unchanged.", flush=True)
        return 130
    finally:
        if desktop:
            desktop.close()
        for event, handler in handlers.items():
            signal.signal(event, handler)
        health.atomic_json(directory / "last_stop.json", {
            "monitor_pid": os.getpid(), "stopped_at_utc": datetime.now(health.UTC).isoformat(),
            "stopped_by": stopped_by, "journal": str(config.journal)})


def supervise_monitor(config: health.HealthConfig, *, interval: float = 5, popups: bool = True) -> int:
    """Keep the alarm responsive if the separate read-only inspector fails."""
    try:
        from polling_monitor_supervisor import MonitorWatchdog, stop_monitor_child
    except ModuleNotFoundError:
        from mb_market_data.polling_monitor_supervisor import MonitorWatchdog, stop_monitor_child
    directory = config.market_data_root / "output/polling_health" / str(config.session_date)
    stop_file = directory / "stop.request"
    child_stop = directory / "inspector.stop.request"
    token = uuid4().hex
    child = desktop = None
    stopped_by = "error"
    print("Independent monitor watchdog. No API, password, or poller restart.", flush=True)
    print("Q stops the monitor and watchdog; production pollers continue.", flush=True)
    try:
        with health.operation_lock("monitor-supervisor:" + str(config.journal).casefold()):
            directory.mkdir(parents=True, exist_ok=True)
            stop_file.unlink(missing_ok=True)
            child_stop.unlink(missing_ok=True)
            command = [sys.executable, str(Path(__file__).resolve()), "monitor", "--market-data-root", str(config.market_data_root),
                       "--session-date", str(config.session_date), "--journal", str(config.journal),
                       "--grace-seconds", str(config.grace_seconds), "--interval", str(interval),
                       "--stop-file", str(child_stop), "--monitor-token", token, "--no-popup"]
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL)
            watchdog = MonitorWatchdog(child_pid=child.pid, token=token, journal=config.journal,
                                       started_monotonic=time.monotonic())
            if os.name == "nt" and popups:
                desktop = alarm.DesktopAlarm()
                desktop.start()
            previous = None
            while True:
                reason = wait_for_stop(0, stop_file)
                if reason:
                    stopped_by = reason
                    return 0
                try:
                    snapshot = json.loads((directory / "latest.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    snapshot = None
                state = watchdog.inspect(snapshot, child_exit=child.poll(), now_monotonic=time.monotonic())
                now = datetime.now(health.UTC)
                state.update(observed_at_utc=now.isoformat(), supervisor_pid=os.getpid(),
                             monitor_token=token, journal=str(config.journal), session_date=str(config.session_date))
                health.atomic_json(directory / "supervisor.json", state)
                if previous != state["state"]:
                    print(f"MONITOR WATCHDOG: {state['state']} | {state['detail']}", flush=True)
                    previous = state["state"]
                current = watchdog.last_snapshot
                if desktop:
                    current = current or {"channels": {}, "observed_at_utc": now.isoformat()}
                    conditions = health.alert_conditions(current) if watchdog.last_snapshot else {}
                    try:
                        incidents = json.loads((directory / "incidents.json").read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        incidents = []
                    payload = alarm.alarm_payload(current, conditions, incidents,
                        window_active=config.start_at <= now < config.stop_at,
                        status_path=directory / "STATUS.txt", poller_windows="prod-poll-uni / prod-poll-focus")
                    if state["state"] in {"STOPPED", "OVERDUE"}:
                        payload["urgent"]["monitor:" + state["state"]] = state["detail"]
                        payload["text"] = (state["detail"] + "\n\nLeave both pollers running. "
                            "Press Q in prod-health, then run 02_prod_health_monitor.cmd again.\n\n" + payload["text"])
                        payload["observed_at_utc"] = now.isoformat()
                    desktop.publish(payload)
                reason = wait_for_stop(1, stop_file)
                if reason:
                    stopped_by = reason
                    return 0
    except KeyboardInterrupt:
        stopped_by = "keyboard interrupt"
        return 130
    finally:
        if child is not None:
            stop_monitor_child(child, child_stop)
        if desktop:
            desktop.close()
        health.atomic_json(directory / "supervisor_stop.json", {
            "supervisor_pid": os.getpid(), "stopped_at_utc": datetime.now(health.UTC).isoformat(),
            "stopped_by": stopped_by, "journal": str(config.journal)})
        print(f"Monitor watchdog stopped by {stopped_by}. Pollers and journal unchanged.", flush=True)


def alarm_demo(root: Path) -> int:
    """Exercise only the desktop alarm, with a fabricated failure and recovery."""
    if os.name != "nt":
        raise ValueError("The desktop alarm demonstration must run on Windows")
    directory = root.resolve() / "output/polling_alarm_demo" / (datetime.now(health.UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8])
    directory.mkdir(parents=True)
    print("SYNTHETIC ALARM ONLY. No production journal, poller, API, or password.", flush=True)
    print("A red topmost window and three tones should repeat every 10 seconds.", flush=True)
    print("After 40 seconds, simulated recovery stops the sound and leaves a quiet notice.", flush=True)
    print("Try Silence for 5 minutes, or wait to hear repeated tones. Dismiss the recovered notice to finish.", flush=True)
    print("Q in this CMD window also stops this demonstration.", flush=True)
    desktop = alarm.DesktopAlarm()
    began, recovered, stopped_by = time.monotonic(), False, "error"
    payload = {"urgent": {"demo:failed": "SYNTHETIC Focus failure"}, "notices": {},
               "text": "SYNTHETIC ALARM TEST\n\nFocus is deliberately shown as failed. Uni is shown as healthy.\n\nThis is a desktop alarm demonstration only.\n\nAfter 40 seconds the simulated failure recovers. The sound stops, while a quiet notice remains until you dismiss it.",
               "observed_at_utc": datetime.now(health.UTC).isoformat(), "status_path": str(directory / "STATUS.txt")}
    try:
        desktop.start()
        while time.monotonic() - began < 300:
            if not recovered and time.monotonic() - began >= 40:
                payload.update(urgent={}, notices={"demo:coverage": "One simulated missed slot remains"},
                               text="SYNTHETIC RECOVERY\n\nFocus is shown as running again. Uni is still healthy.\n\nThe repeating sound has stopped. One simulated missed slot remains for later review.\n\nDismiss this notice to finish the demonstration.")
                recovered = True
                print("SYNTHETIC recovery: urgent sound stopped; notice remains for acknowledgement.", flush=True)
            payload["observed_at_utc"] = datetime.now(health.UTC).isoformat()
            (directory / "STATUS.txt").write_text(payload["text"], encoding="utf-8")
            desktop.publish(dict(payload))
            if recovered and not desktop.view()["visible"]:
                stopped_by = "recovered notice dismissed"
                break
            if wait_for_stop(0.2):
                stopped_by = "Q key"
                break
        else:
            stopped_by = "five-minute demonstration limit"
    except KeyboardInterrupt:
        stopped_by = "keyboard interrupt"
    finally:
        desktop.close()
        health.atomic_json(directory / "demo.json", {"synthetic": True, "stopped_by": stopped_by,
                          "simulated_recovery_shown": recovered, "sound_sequences_requested": desktop.view()["sound_count"]})
    print(f"Alarm demonstration finished: {stopped_by}. Report whether you saw the window and heard repeated tones.", flush=True)
    return 0


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
    parser.add_argument("operation", choices=("monitor", "restart", "windows-check", "stop-monitor", "alarm-demo"))
    parser.add_argument("--market-data-root", type=Path, required=True)
    parser.add_argument("--session-date", type=date.fromisoformat)
    parser.add_argument("--supervise", action="store_true", help="Run the health inspector under an independent watchdog")
    parser.add_argument("--monitor-token", help=argparse.SUPPRESS)
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
        if args.operation == "alarm-demo":
            return alarm_demo(args.market_data_root)
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
        if args.supervise:
            return supervise_monitor(config, interval=args.interval, popups=not args.no_popup)
        return monitor(config, once=args.once, interval=args.interval, popups=not args.no_popup,
                       stop_file=args.stop_file, keyboard=args.monitor_token is None, monitor_token=args.monitor_token)
    except KeyboardInterrupt:
        print("\nStopped by operator. Check the affected poller window if a restart was in progress.")
        return 130
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Polling health tool STOPPED: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
