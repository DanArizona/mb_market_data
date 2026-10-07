"""Independent, read-only supervision of schema-v2 quote polling.

No market-quote timestamp is used as a poller heartbeat. A journaled slot or
empty-membership skip is the durable evidence of successful polling. The
observer never initializes, writes, checkpoints, or repairs the daily journal.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
CADENCES = {"uni": (0, 30), "focus": (5, 20, 35, 50)}


def timestamp(value: str | datetime) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("A timestamp must include its timezone")
    return result.astimezone(UTC)


@dataclass(frozen=True)
class HealthConfig:
    market_data_root: Path
    journal: Path
    session_date: date
    start_at: datetime
    stop_at: datetime
    grace_seconds: float = 90.0
    read_timeout_seconds: float = 0.25

    def __post_init__(self) -> None:
        start, stop = timestamp(self.start_at), timestamp(self.stop_at)
        if stop <= start or start.astimezone(ET).date() != self.session_date:
            raise ValueError("Invalid polling window or session date")
        if stop.astimezone(ET).date() != self.session_date:
            raise ValueError("The polling window must stay within the session date")
        if self.grace_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("Timeout and grace must be positive")
        if Path(self.journal).name != f"{self.session_date}.sqlite3":
            raise ValueError("Journal filename must match the session date")


def configured_slots(config: HealthConfig, channel: str) -> Iterator[datetime]:
    current = timestamp(config.start_at).replace(second=0, microsecond=0)
    stop = timestamp(config.stop_at)
    while current < stop:
        for second in CADENCES[channel]:
            slot = current + timedelta(seconds=second)
            if timestamp(config.start_at) <= slot < stop:
                yield slot
        current += timedelta(minutes=1)


def read_journal(config: HealthConfig) -> dict[str, Any]:
    """Read small acquisition headers in one bounded, consistent snapshot.

    mode=ro honors the live WAL. immutable=1 is deliberately never used.
    Contextlib.closing closes the actual connection on Windows too.
    """
    path = Path(config.journal).resolve()
    deadline = time.monotonic() + 1.0
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro",
                                uri=True, timeout=config.read_timeout_seconds)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        connection.execute("BEGIN")
        identity = connection.execute("SELECT * FROM store_identity").fetchone()
        if identity is None or identity["schema_version"] != 2 or identity["session_date"] != str(config.session_date):
            raise ValueError("Observer requires the selected session's schema-v2 journal")
        runs = [dict(row) for row in connection.execute("SELECT * FROM poll_run ORDER BY started_at_utc")]
        acquisitions = [dict(row) for row in connection.execute(
            "SELECT acquisition_id,run_id,channel,scheduled_at_utc,completed_at_utc,requested_symbol_count "
            "FROM quote_acquisition WHERE session_date=? ORDER BY scheduled_at_utc", (str(config.session_date),))]
        skips = [dict(row) for row in connection.execute(
            "SELECT run_id,channel,scheduled_at_utc,observed_at_utc,reason FROM poll_slot_skip "
            "WHERE session_date=? ORDER BY scheduled_at_utc", (str(config.session_date),))]
        revisions = [dict(row) for row in connection.execute(
            "SELECT channel,revision,effective_at_utc,symbol_count FROM sampling_channel_revision "
            "WHERE session_date=? ORDER BY effective_at_utc,revision", (str(config.session_date),))]
        outcomes = {}
        for channel in CADENCES:
            latest = next((row for row in reversed(acquisitions) if row["channel"] == channel), None)
            outcomes[channel] = {} if latest is None else dict(connection.execute(
                "SELECT status,count(*) FROM quote_observation WHERE acquisition_id=? GROUP BY status",
                (latest["acquisition_id"],)).fetchall())
        connection.rollback()
    return {"runs": runs, "acquisitions": acquisitions, "skips": skips,
            "revisions": revisions, "latest_outcomes": outcomes}


def _tail(path: Path, limit: int = 8192) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - limit))
        return stream.read(limit).decode("utf-8", errors="replace").strip()


def run_evidence(run: dict[str, Any]) -> dict[str, Any]:
    config = json.loads(run["configuration_json"])
    directory = Path(config["evidence_run_directory"])
    result = {"directory": str(directory), "manifest": None, "error_tail": "", "read_error": None}
    try:
        with (directory / "manifest.json").open(encoding="utf-8") as stream:
            manifest = json.load(stream)
        if manifest.get("journal_run_id") != run["run_id"] or manifest.get("journal_schema_version") != 2:
            raise ValueError("Polling manifest identity does not match the journal run")
        result["manifest"] = manifest
        result["error_tail"] = _tail(directory / "errors.log")
    except (OSError, ValueError) as error:
        result["read_error"] = f"{type(error).__name__}: {error}"
    return result


def _flag(argv: list[str], name: str) -> str | None:
    for index, value in enumerate(argv):
        if value == name and index + 1 < len(argv):
            return argv[index + 1]
        if value.startswith(name + "="):
            return value[len(name) + 1:]
    return None


def matching_processes(config: HealthConfig, channel: str, processes: list[dict[str, Any]]) -> list[int]:
    """Conservative duplicate guard, including relative command-line paths.

    CIM does not report a working directory. A same-session relative path
    match is therefore a candidate, sufficient to REFUSE a second launch.
    Successful journal commits, rather than PID presence, establish health.
    """
    matches = []
    for process in processes:
        argv = process.get("argv", [])
        if not any(value.replace("\\", "/").rsplit("/", 1)[-1].lower() == "probe_universe_quote_watch.py" for value in argv):
            continue
        if _flag(argv, "--watchlist-kind") != channel:
            continue
        if _flag(argv, "--journal-schema-version") != "2":
            # Unknown/legacy invocation for this channel cannot prove absence.
            matches.append(int(process["pid"]))
            continue
        root = _flag(argv, "--journal-root")
        if root is None:
            matches.append(int(process["pid"]))
            continue
        start = _flag(argv, "--start-at")
        try:
            if start is not None and timestamp(start).astimezone(ET).date() != config.session_date:
                continue
        except ValueError:
            matches.append(int(process["pid"]))
            continue
        # ntpath supports Windows absolute/relative paths even in offline tests.
        import ntpath
        expected = ntpath.normcase(ntpath.normpath(str(config.journal.parent)))
        supplied = ntpath.normcase(ntpath.normpath(root))
        if not ntpath.isabs(supplied):
            supplied = ntpath.normcase(ntpath.normpath(ntpath.join(str(config.market_data_root), supplied)))
        if supplied == expected:
            matches.append(int(process["pid"]))
    return sorted(matches)


def windows_process_rows() -> list[dict[str, Any]]:
    """Recheck blank command lines within one shared eight-second deadline.

    A process is excluded only when the second CIM query confirms its PID is
    absent. A still-present, unreadable process continues to block restart.
    """
    deadline = time.monotonic() + 8

    def query(where: str) -> list[dict[str, Any]]:
        script = ("$ErrorActionPreference='Stop'; @((Get-CimInstance Win32_Process " + where +
                  " | Select-Object ProcessId,CommandLine)) | ConvertTo-Json -Compress")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("Windows process inspection deadline expired")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, timeout=remaining, check=True)
        raw = json.loads(result.stdout.strip() or "[]")
        raw = [] if raw is None else [raw] if isinstance(raw, dict) else raw
        if not isinstance(raw, list) or any(not isinstance(x, dict) or int(x["ProcessId"]) <= 0 for x in raw):
            raise ValueError("Malformed Windows process inspection response")
        return raw

    raw = query("| Where-Object { $_.Name -match '^(python|pythonw|python3.*)\\.exe$' }")
    unreadable = [int(item["ProcessId"]) for item in raw if not str(item.get("CommandLine") or "").strip()]
    if unreadable:
        # Query these exact PIDs without filtering on name. PID reuse or a
        # still-present protected process cannot masquerade as absence.
        again = query('-Filter "' + " OR ".join(f"ProcessId={pid}" for pid in unreadable) + '"')
        raw = [item for item in raw if int(item["ProcessId"]) not in unreadable] + again
        for item in again:
            if int(item["ProcessId"]) not in unreadable:
                raise ValueError("Unexpected PID returned during process recheck")
            if not str(item.get("CommandLine") or "").strip():
                raise ValueError(f"Python process {item['ProcessId']} has an unreadable command line after recheck")
    return raw


def windows_process_inventory() -> dict[str, Any]:
    """Fail closed when process absence cannot be established."""
    if os.name != "nt":
        return {"known": False, "processes": [], "error": "Windows CIM process inspection is required"}
    try:
        raw = windows_process_rows()
        import ctypes
        from ctypes import wintypes
        parse = ctypes.windll.shell32.CommandLineToArgvW
        parse.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        parse.restype = ctypes.POINTER(wintypes.LPWSTR)
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        processes = []
        for item in raw:
            if not item.get("CommandLine"):
                raise ValueError(f"Python process {item['ProcessId']} has an unreadable command line")
            count = ctypes.c_int()
            pointer = parse(item["CommandLine"], ctypes.byref(count))
            if not pointer:
                raise OSError("CommandLineToArgvW failed")
            try:
                argv = [pointer[i] for i in range(count.value)]
            finally:
                free(pointer)
            processes.append({"pid": int(item["ProcessId"]), "argv": argv})
        return {"known": True, "processes": processes, "error": None}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        return {"known": False, "processes": [], "error": f"{type(error).__name__}: {error}"}


def inspect_health(config: HealthConfig, *, now: datetime | None = None,
                   inventory: dict[str, Any] | None = None) -> dict[str, Any]:
    inventory = inventory if inventory is not None else windows_process_inventory()
    now = timestamp(now or datetime.now(UTC))
    result = {"version": 1, "observed_at_utc": now.isoformat(), "session_date": str(config.session_date),
              "journal": str(Path(config.journal).resolve()), "grace_seconds": config.grace_seconds,
              "process_inspection": {"known": inventory["known"], "error": inventory.get("error")},
              "journal_read": "OK", "journal_error": None, "channels": {}}
    began = time.monotonic()
    try:
        data = read_journal(config)
        for run in data["runs"]:
            configuration = json.loads(run["configuration_json"])
            if configuration.get("watchlist_kind") in CADENCES:
                if (timestamp(configuration["poll_window_start_et"]) != timestamp(config.start_at)
                        or timestamp(configuration["poll_window_end_et"]) != timestamp(config.stop_at)):
                    raise ValueError("Registered run differs from the selected polling window")
    except (OSError, ValueError, sqlite3.Error) as error:
        result.update(journal_read="UNAVAILABLE", journal_error=f"{type(error).__name__}: {error}")
        result["journal_read_seconds"] = round(time.monotonic() - began, 6)
        return result
    result["journal_read_seconds"] = round(time.monotonic() - began, 6)
    for channel in CADENCES:
        runs = []
        for run in data["runs"]:
            configuration = json.loads(run["configuration_json"])
            if configuration.get("watchlist_kind") == channel:
                if (timestamp(configuration["poll_window_start_et"]) != timestamp(config.start_at)
                        or timestamp(configuration["poll_window_end_et"]) != timestamp(config.stop_at)):
                    raise ValueError(f"{channel} run has a different configured polling window")
                runs.append(run)
        run = runs[-1] if runs else None
        evidence = run_evidence(run) if run else None
        manifest = evidence["manifest"] if evidence else None
        committed = [row for row in data["acquisitions"] if row["channel"] == channel]
        skips = [row for row in data["skips"] if row["channel"] == channel]
        fulfilled = {timestamp(row["scheduled_at_utc"]) for row in committed + skips}
        scheduled = list(configured_slots(config, channel))
        final_slot = scheduled[-1] if scheduled else None
        due = [slot for slot in scheduled if slot + timedelta(seconds=config.grace_seconds) <= now]
        missing = [slot.isoformat() for slot in due if slot not in fulfilled]
        latest = committed[-1] if committed else None
        latest_skip = skips[-1] if skips else None
        activity = max(fulfilled, default=None)
        current_commits = [row for row in committed + skips if run and row["run_id"] == run["run_id"]]
        current_due = [slot for slot in due if run and slot >= timestamp(run["started_at_utc"])]
        pids = matching_processes(config, channel, inventory["processes"]) if inventory["known"] else []
        active = timestamp(config.start_at) <= now < timestamp(config.stop_at)
        state, reason = "RUNNING", "Recent scheduled outcomes are committed"
        if run is None:
            state, reason = "NOT_STARTED", "No polling run is registered for this channel"
        elif evidence["read_error"]:
            state, reason = "EVIDENCE_UNAVAILABLE", evidence["read_error"]
        elif manifest.get("journal_errors", 0) or "JournalWriteError" in evidence["error_tail"] or "MembershipUnavailableError" in evidence["error_tail"]:
            state, reason = "FAILED", evidence["error_tail"] or "Polling manifest reports a journal failure"
        elif manifest.get("completed_at_et") and final_slot is not None and timestamp(manifest["completed_at_et"]) < final_slot:
            state, reason = "STOPPED_EARLY", "Poller finished before its final scheduled slot"
        elif manifest.get("completed_at_et") and final_slot is not None and final_slot not in fulfilled:
            state, reason = "INCOMPLETE_FINISH", "Poller finished without committing its final scheduled slot"
        elif manifest.get("completed_at_et"):
            state = "WINDOW_ENDED" if now >= timestamp(config.stop_at) else "SCHEDULE_COMPLETE"
            reason = "Poller finished after committing its final scheduled slot; inspect full-session coverage"
        elif now < timestamp(config.start_at):
            state, reason = "WAITING", "Waiting for the configured opening slot"
        elif active and inventory["known"] and not pids:
            state, reason = "PROCESS_MISSING", "No matching Python poller process was found"
        elif len(pids) > 1:
            state, reason = "DUPLICATE_PROCESSES", "More than one candidate poller matches this channel and journal"
        elif active and not current_commits:
            if current_due:
                state, reason = "OVERDUE", "The current run's first due slot has no committed acquisition or empty-membership skip"
            else:
                state, reason = "AWAITING_COMMIT", "Current run is waiting for its first scheduled commit; recovery is not confirmed"
        elif active and current_due and (activity is None or activity < current_due[-1]):
            state, reason = "OVERDUE", "The latest due slot has no committed acquisition or empty-membership skip"
        elif now >= timestamp(config.stop_at):
            state, reason = "WINDOW_ENDED", "Configured polling window has ended; inspect final coverage"
        elif not inventory["known"]:
            state, reason = "PROCESS_UNKNOWN", "Journal commits can be checked, but process inspection is unavailable"
        revisions = [row for row in data["revisions"] if row["channel"] == channel and timestamp(row["effective_at_utc"]) <= now]
        result["channels"][channel] = {
            "state": state, "reason": reason, "latest_run_id": run["run_id"] if run else None,
            "candidate_pids": pids, "committed_acquisitions": len(committed), "committed_skips": len(skips),
            "latest_run_committed_slots": len(current_commits),
            "last_committed_slot_utc": activity.isoformat() if activity else None,
            "last_acquisition_slot_utc": latest["scheduled_at_utc"] if latest else None,
            "last_acquisition_completed_at_utc": latest["completed_at_utc"] if latest else None,
            "last_skip_slot_utc": latest_skip["scheduled_at_utc"] if latest_skip else None,
            "current_members": revisions[-1]["symbol_count"] if revisions else None,
            "latest_outcomes": data["latest_outcomes"][channel], "due_slots": len(due),
            "missing_slots": missing, "missing_slot_count": len(missing),
            "evidence_directory": evidence["directory"] if evidence else None,
            "error_tail": evidence["error_tail"] if evidence else "", "polling_window_active": active,
            "restart_defaults_supported": (manifest is None or
                (manifest.get("fields", "all") == "all" and manifest.get("batch_size", 400) == 400
                 and manifest.get("timeout_seconds", 20) == 20 and manifest.get("max_samples") is None)),
        }
    return result


def restart_command(config: HealthConfig, channel: str, health: dict[str, Any],
                    inventory: dict[str, Any], *, now: datetime | None = None,
                    executable: str = sys.executable) -> list[str]:
    """Prepare one manual restart; never kill a process or alter old records."""
    now = timestamp(now or datetime.now(UTC))
    if channel not in CADENCES:
        raise ValueError("Unsupported polling channel")
    if not timestamp(config.start_at) <= now < timestamp(config.stop_at):
        raise ValueError("Manual recovery is restricted to the active session window")
    if not inventory["known"]:
        raise ValueError("Restart refused: process inspection is unavailable")
    matches = matching_processes(config, channel, inventory["processes"])
    if matches:
        raise ValueError(f"Restart refused: candidate poller PID(s) {matches} already exist")
    if health["journal_read"] != "OK":
        raise ValueError("Restart refused: journal cannot be read; resolve that problem first")
    current = health["channels"][channel]
    if not current["restart_defaults_supported"]:
        raise ValueError("Restart refused: custom poller options need review before using this default launcher")
    if current["state"] not in {"FAILED", "STOPPED_EARLY", "PROCESS_MISSING", "OVERDUE", "NOT_STARTED"}:
        raise ValueError(f"Restart refused: channel state is {current['state']}")
    probe = Path(config.market_data_root).resolve() / "probes/probe_universe_quote_watch.py"
    if not probe.is_file():
        raise ValueError("Production poller script was not found")
    return [executable, str(probe), "--watchlist-kind", channel,
            "--start-at", config.start_at.astimezone(ET).isoformat(),
            "--stop-at", config.stop_at.astimezone(ET).isoformat(),
            "--journal-root", str(Path(config.journal).resolve().parent), "--journal-schema-version", "2"]


def check_write_available(config: HealthConfig) -> None:
    """Manual restart only: briefly reserve a write transaction, then roll back.

    No data is written. A readable WAL is not proof that its writer lock has
    cleared. Fail before launching if a writer reservation is still blocked.
    """
    with closing(sqlite3.connect(Path(config.journal).resolve().as_uri() + "?mode=rw",
                                uri=True, timeout=config.read_timeout_seconds)) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as error:
            raise ValueError(f"Restart refused: journal write access is still unavailable: {error}") from error
        finally:
            connection.rollback()


@contextmanager
def operation_lock(key: str) -> Iterator[None]:
    """OS-owned lock survives neither a crash nor process exit; no stale PID file."""
    digest = hashlib.sha256(key.encode()).hexdigest()
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateMutexW(None, False, "Local\\mb_polling_" + digest)
        if not handle:
            raise OSError("Could not create operation mutex")
        acquired = False
        try:
            acquired = kernel.WaitForSingleObject(handle, 0) in (0, 0x80)
            if not acquired:
                raise ValueError("Another monitor or guided restart already owns this operation")
            yield
        finally:
            if acquired:
                kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)
    else:
        import fcntl
        import tempfile
        with (Path(tempfile.gettempdir()) / ("mb_polling_" + digest + ".lock")).open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("Another monitor or guided restart already owns this operation") from error
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def atomic_json(path: Path, value: Any) -> None:
    from uuid import uuid4
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def alert_conditions(health: dict[str, Any]) -> dict[str, str]:
    alerts = {}
    if health["journal_read"] != "OK":
        alerts["journal:unavailable"] = health["journal_error"]
    if not health["process_inspection"]["known"]:
        alerts["process:unknown"] = health["process_inspection"]["error"]
    for channel, info in health["channels"].items():
        if info["state"] not in {"RUNNING", "WAITING", "AWAITING_COMMIT", "SCHEDULE_COMPLETE", "WINDOW_ENDED", "PROCESS_UNKNOWN"}:
            alerts[f"{channel}:{info['state']}:{info['latest_run_id']}"] = info["reason"]
        if info["missing_slot_count"]:
            alerts[f"{channel}:coverage"] = f"{info['missing_slot_count']} scheduled slots remain missing after grace"
        if info["latest_outcomes"].get("request_error", 0):
            alerts[f"{channel}:request_error"] = f"Latest committed acquisition has {info['latest_outcomes']['request_error']} request-error outcomes"
    return alerts


def persist_health(directory: Path, health: dict[str, Any]) -> list[dict[str, Any]]:
    """Retain incident history across monitor restarts and recovery commits."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "incidents.json"
    history = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    active = alert_conditions(health)
    existing = {item["key"]: item for item in history if item["resolved_at_utc"] is None}
    new = []
    for key, detail in active.items():
        if key not in existing:
            item = {"key": key, "first_detected_at_utc": health["observed_at_utc"],
                    "last_detected_at_utc": health["observed_at_utc"], "detail": detail,
                    "resolved_at_utc": None}
            history.append(item)
            new.append(dict(item))
        else:
            existing[key].update(detail=detail, last_detected_at_utc=health["observed_at_utc"])
    # A read failure provides no evidence that a channel recovered.
    if health["journal_read"] == "OK":
        for key, item in existing.items():
            if key not in active:
                channel = key.split(":", 1)[0]
                info = health["channels"].get(channel)
                if info is None or (info["state"] in {"RUNNING", "SCHEDULE_COMPLETE", "WINDOW_ENDED"}
                                    and info["latest_run_committed_slots"] > 0):
                    item["resolved_at_utc"] = health["observed_at_utc"]
    atomic_json(path, history)
    atomic_json(directory / "latest.json", health)
    summary = format_health(health)
    from uuid import uuid4
    temporary = directory / ("STATUS." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(summary, encoding="utf-8")
        os.replace(temporary, directory / "STATUS.txt")
    finally:
        temporary.unlink(missing_ok=True)
    if new:
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            for event in new:
                stream.write(json.dumps(event, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return new


def format_health(health: dict[str, Any]) -> str:
    def et(value: str | None) -> str:
        return timestamp(value).astimezone(ET).strftime("%H:%M:%S ET") if value else "none"
    lines = [f"Polling health: {health['session_date']} as of {et(health['observed_at_utc'])}",
             f"Journal: {health['journal']}", f"Journal read: {health['journal_read']} ({health['journal_read_seconds']:.3f}s)",
             f"Slot grace: {health['grace_seconds']:g}s; trade/quote age is not a poller heartbeat."]
    if health["journal_error"]:
        lines.append("ALERT: " + health["journal_error"])
    if not health["process_inspection"]["known"]:
        lines.append("PROCESS INSPECTION UNAVAILABLE: " + str(health["process_inspection"]["error"]))
    for channel, info in health["channels"].items():
        lines.extend(["", f"{channel.upper()}: {info['state']} | candidate PID(s): {info['candidate_pids']}",
                      info["reason"], f"Last committed slot: {et(info['last_committed_slot_utc'])}; "
                      f"acquisitions: {info['committed_acquisitions']}; empty-membership skips: {info['committed_skips']}",
                      f"Missing slots after grace: {info['missing_slot_count']}; members: {info['current_members']}"])
        if info["missing_slots"]:
            lines.append("Gap range: " + et(info["missing_slots"][0]) + " through " + et(info["missing_slots"][-1]))
        if info["error_tail"]:
            lines.append("Saved error: " + info["error_tail"])
    lines.extend(["", "If a channel stops: leave healthy channels running. Check its CMD error first.",
                  "Use the matching guided restart launcher in that channel's CMD window.",
                  "Restart restores future slots. Salvage saved acquisitions after the window closes.", ""])
    return "\n".join(lines)
