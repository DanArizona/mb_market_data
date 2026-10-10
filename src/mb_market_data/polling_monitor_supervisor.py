"""Watch one read-only health monitor child; never start or stop a poller."""
from __future__ import annotations

import subprocess
import json
import os
from uuid import uuid4
from datetime import datetime
from pathlib import Path


class MonitorIncidentHistory:
    """Append-only monitor incidents, including acknowledgements across restart."""

    def __init__(self, path: Path):
        self.path = path
        self.records = {}
        self.pending = []
        self.read_error = None
        try:
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    item = json.loads(line)
                    for key in ("id", "key", "detail", "first_detected_at_utc", "resolved_at_utc", "acknowledged_at_utc"):
                        if key not in item:
                            raise ValueError("Incomplete monitor incident")
                    self.records[item["id"]] = item
        except (OSError, ValueError, TypeError) as error:
            self.read_error = f"Monitor incident history could not be read: {type(error).__name__}: {error}"

    def update(self, active: dict, when: str, *, can_resolve: bool = True) -> None:
        existing = {item["key"]: item for item in self.records.values() if item["resolved_at_utc"] is None}
        for key, detail in active.items():
            if key not in existing:
                item = {"id": uuid4().hex, "key": key, "detail": detail,
                        "first_detected_at_utc": when, "resolved_at_utc": None, "acknowledged_at_utc": None}
                self.records[item["id"]] = item
                self.pending.append(dict(item))
        for key, item in existing.items():
            # A replacement process alone does not establish monitor recovery.
            if key not in active and (can_resolve or key not in {"monitor:STOPPED", "monitor:OVERDUE"}):
                item["resolved_at_utc"] = when
                self.pending.append(dict(item))

    def acknowledge(self, when: str) -> None:
        for item in self.records.values():
            if item["resolved_at_utc"] and not item["acknowledged_at_utc"]:
                item["acknowledged_at_utc"] = when
                self.pending.append(dict(item))

    def flush(self) -> str | None:
        if not self.pending:
            return None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write("".join(json.dumps(item, sort_keys=True) + "\n" for item in self.pending))
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as error:
            return f"Monitor incident history could not be saved: {type(error).__name__}: {error}"
        self.pending.clear()
        return None

    def urgent(self) -> dict:
        return {item["key"]: item["detail"] for item in self.records.values() if item["resolved_at_utc"] is None}

    def notices(self) -> dict:
        return {"monitor:recovered:" + item["id"]:
                f"Earlier monitor alert at {item['first_detected_at_utc']}: {item['detail']}. "
                f"Recovery confirmed at {item['resolved_at_utc']}."
                for item in self.records.values() if item["resolved_at_utc"] and not item["acknowledged_at_utc"]}


class MonitorWatchdog:
    def __init__(self, *, child_pid: int, token: str, journal: Path,
                 started_monotonic: float, timeout_seconds: float = 30):
        self.child_pid = child_pid
        self.token = token
        self.journal = journal.resolve()
        self.timeout_seconds = timeout_seconds
        self.last_progress = started_monotonic
        self.last_observed = None
        self.last_snapshot = None

    def inspect(self, snapshot: dict | None, *, child_exit: int | None, now_monotonic: float) -> dict:
        valid = False
        if snapshot is not None:
            try:
                valid = (snapshot["monitor_pid"] == self.child_pid
                         and snapshot["monitor_token"] == self.token
                         and Path(snapshot["journal"]).resolve() == self.journal)
                observed = datetime.fromisoformat(snapshot["observed_at_utc"].replace("Z", "+00:00"))
                valid = valid and observed.tzinfo is not None
            except (KeyError, ValueError, TypeError):
                valid = False
        if valid and (self.last_observed is None or observed > self.last_observed):
            self.last_progress = now_monotonic
            self.last_observed = observed
            self.last_snapshot = snapshot
        if child_exit is not None:
            state, detail = "STOPPED", f"Health monitor exited with code {child_exit}."
        elif now_monotonic - self.last_progress > self.timeout_seconds:
            state, detail = "OVERDUE", "Health monitor has stopped publishing completed checks."
        elif self.last_snapshot is None:
            state, detail = "STARTING", "Waiting for the monitor's first completed check."
        else:
            state, detail = "RUNNING", "Health monitor is publishing completed checks."
        return {"state": state, "detail": detail, "child_pid": self.child_pid,
                "last_observed_at_utc": self.last_observed.isoformat() if self.last_observed else None}


def stop_monitor_child(child: subprocess.Popen, stop_file: Path, *, grace_seconds: float = 10) -> None:
    # This is the exact child this supervisor launched, never a poller PID.
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        stop_file.write_text("{}\n", encoding="utf-8")
    except OSError:
        if child.poll() is None:
            child.terminate()  # only this supervisor's exact inspector child
    try:
        child.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)
