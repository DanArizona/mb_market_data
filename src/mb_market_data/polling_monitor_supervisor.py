"""Watch one read-only health monitor child; never start or stop a poller."""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path


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
    stop_file.write_text("{}\n", encoding="utf-8")
    try:
        child.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)
