"""Bounded, observable heartbeat checks for the daily session's own workers.

An I/O failure is not an operator stop. A previously verified heartbeat remains
usable only until its ORIGINAL 30-second freshness limit. Retrying/reading the
same timestamp does not renew it. Wrong ownership or explicit stop wins at once.
No credentials, provider operations, or journal mutation occur in this module.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import time
from typing import Callable

MAX_HEARTBEAT_AGE = 30.0
READ_DELAYS = (0.01, 0.02, 0.04)  # four attempts, at most 70 ms explicit sleep
FUTURE_CLOCK_TOLERANCE = 1.0


def safe_error(error: BaseException) -> dict:
    """Never log exception messages or file contents; both can contain secrets."""
    return {"error_type": type(error).__name__,
            "errno": getattr(error, "errno", None),
            "winerror": getattr(error, "winerror", None)}


def append_control_event(path: Path, value: dict) -> None:
    """One child owns this generation's diagnostic file. Failures don't stop it."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, sort_keys=True) + "\n")
    except OSError as error:
        try:
            print("WORKER DIAGNOSTIC WRITE UNAVAILABLE:", type(error).__name__, flush=True)
        except (OSError, ValueError):
            pass


class OwnerHeartbeatGuard:
    """Remember a validated receipt, never an indefinitely cached boolean."""

    def __init__(self, root: Path, *, pid: int | None, reader: Callable,
                 clock: Callable[[], datetime], monotonic: Callable = time.monotonic,
                 sleep: Callable = time.sleep, emit: Callable | None = None):
        self.path = Path(root) / "owner.json"
        self.pid, self.reader, self.clock = pid, reader, clock
        self.monotonic, self.sleep, self.emit = monotonic, sleep, emit
        self.reason = None
        self.last_at = None
        self.last_mono = None
        self.age_at_last = 0.0
        self.read_errors = 0
        self.failure_since = None

    def _event(self, event: str, **fields) -> None:
        if self.emit is not None:
            try:
                self.emit({"event": event, "at": self.clock().isoformat(),
                           "expected_owner_pid": self.pid, **fields})
            except (OSError, ValueError):
                pass  # diagnostics must not change the ownership verdict


    def _expired(self) -> bool:
        if self.last_at is None:
            return True
        # Monotonic elapsed prevents a backwards wall-clock change or repeated
        # reading of the same file from renewing permission indefinitely.
        wall_age = (self.clock() - self.last_at).total_seconds()
        elapsed_age = self.age_at_last + max(0.0, self.monotonic() - self.last_mono)
        return max(wall_age, elapsed_age) >= MAX_HEARTBEAT_AGE

    def _stop(self, reason: str, **details) -> bool:
        if self.reason is None:
            self.reason = reason
            self._event("owner_check_stopped", reason=reason,
                        last_verified_heartbeat=(self.last_at.isoformat() if self.last_at else None),
                        read_error_count=self.read_errors, **details)
        return False

    def available(self) -> bool:
        if self.reason is not None:
            return False
        receipt = None
        for attempt in range(len(READ_DELAYS) + 1):
            try:
                receipt = self.reader(self.path)
                break
            except (OSError, ValueError, UnicodeError) as error:
                self.read_errors += 1
                if self.failure_since is None:
                    self.failure_since = self.monotonic()
                    self._event("heartbeat_read_retry", **safe_error(error))
                if attempt < len(READ_DELAYS):
                    self.sleep(READ_DELAYS[attempt])
                else:
                    if self.last_at is not None and not self._expired():
                        # Do not update last_at/last_mono from a failed read.
                        return True
                    return self._stop(
                        "owner_heartbeat_unreadable" if self.last_at is None else "owner_heartbeat_expired",
                        **safe_error(error))
        try:
            if not isinstance(receipt, dict):
                raise ValueError("Invalid heartbeat shape")
            owner_pid, active = receipt["pid"], receipt["active"]
            if type(owner_pid) is not int or owner_pid <= 0 or type(active) is not bool:
                raise ValueError("Invalid owner fields")
            at = datetime.fromisoformat(receipt["at"])
            if at.tzinfo is None or at.utcoffset() is None:
                raise ValueError("Naive heartbeat")
        except (KeyError, TypeError, ValueError, OverflowError):
            return self._stop("owner_heartbeat_invalid")
        if self.pid is not None and owner_pid != self.pid:
            return self._stop("owner_replaced", observed_owner_pid=owner_pid)
        if not active:
            return self._stop("owner_inactive")
        current = self.clock()
        age = (current - at).total_seconds()
        if age < -FUTURE_CLOCK_TOLERANCE:
            return self._stop("owner_heartbeat_in_future")
        if self.last_at is not None and at < self.last_at:
            return self._stop("owner_heartbeat_regressed")
        if self.last_at is None or at > self.last_at:
            self.last_at, self.last_mono = at, self.monotonic()
            self.age_at_last = max(0.0, age)
        if self._expired():
            return self._stop("owner_heartbeat_expired")
        if self.failure_since is not None:
            self._event("heartbeat_read_recovered", read_error_count=self.read_errors,
                        interruption_seconds=max(0.0, self.monotonic()-self.failure_since),
                        verified_heartbeat_at=at.isoformat())
            self.failure_since = None
        return True


class WorkerControl:
    """Latched, named stop condition shared by scheduler and pre-call checks."""

    def __init__(self, root: Path, *, owner_pid: int | None, channel: str,
                 generation: int, reader: Callable, clock: Callable[[], datetime],
                 monotonic: Callable = time.monotonic, sleep: Callable = time.sleep,
                 honor_collection_stop: bool = True):
        self.root, self.clock = Path(root), clock
        self.channel, self.generation = channel, generation
        self.honor_collection_stop = honor_collection_stop
        self.reason = None
        self.events_path = self.root / "workers" / f"{channel}-{generation}" / "control-events.jsonl"
        self.guard = OwnerHeartbeatGuard(root, pid=owner_pid, reader=reader,
            clock=clock, monotonic=monotonic, sleep=sleep, emit=self._event)

    def _event(self, value: dict) -> None:
        value = {"at": self.clock().isoformat(), "worker_pid": os.getpid(),
                 "channel": self.channel, "generation": self.generation, **value}
        append_control_event(self.events_path, value)
        try:
            print("DAILY WORKER CONTROL:", value["event"], value.get("reason", ""), flush=True)
        except (OSError, ValueError):
            pass

    def __call__(self) -> bool:
        if self.reason is not None:
            return True
        if (self.root / "exit.request").exists():
            self.reason = "operator_exit_session"
        elif self.honor_collection_stop and (self.root / "stop.request").exists():
            self.reason = "collection_stop_requested"
        elif not self.guard.available():
            self.reason = self.guard.reason
        if self.reason is not None:
            self._event({"event": "worker_stop_requested", "reason": self.reason})
        return self.reason is not None
