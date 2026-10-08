"""Bounded retries of complete journal transactions, never API acquisitions."""
from __future__ import annotations

import math
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class WriteRetryPolicy:
    total_seconds: float = 5.0
    attempt_seconds: float = 0.25
    max_attempts: int = 20
    initial_delay_seconds: float = 0.05
    maximum_delay_seconds: float = 0.25

    def __post_init__(self) -> None:
        for name in ("total_seconds", "attempt_seconds", "initial_delay_seconds", "maximum_delay_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")


class WriteRetryExhausted(sqlite3.OperationalError):
    """The acquired payload remains evidence, but no write was confirmed."""
    def __init__(self, diagnostic: dict[str, Any]):
        self.diagnostic = diagnostic
        self.sqlite_errorcode = diagnostic["sqlite_errorcode"]
        self.sqlite_errorname = diagnostic["sqlite_errorname"]
        super().__init__(
            f"{diagnostic['operation']} exhausted write recovery: "
            f"{diagnostic['attempts']} attempts in {diagnostic['elapsed_seconds']:.3f}s; "
            f"{diagnostic['sqlite_errorname']} ({diagnostic['sqlite_errorcode']}): "
            f"{diagnostic['error']}; database={diagnostic['database']}"
        )


def write_with_recovery(
    attempt: Callable[[float], T], *, policy: WriteRetryPolicy,
    database: Path, operation: str, identity: dict[str, Any],
    notify: Callable[[dict[str, Any]], None] | None = None,
) -> T:
    """Each attempt must roll back and close its connection before raising.

    Only SQLITE_BUSY (including its extended codes) is retried. SQLITE_LOCKED
    describes a same-connection/shared-cache problem and is surfaced instead.
    The deadline bounds contention waits; it never interrupts a successful
    transaction or changes a sample's acquisition timestamps or identifiers.
    """
    started = time.monotonic()
    deadline = started + policy.total_seconds
    attempts = 0
    attempt_started_elapsed = 0.0
    last_error: sqlite3.Error | None = None

    def diagnostic(state: str) -> dict[str, Any]:
        return {
            "version": 1, "observed_at_utc": datetime.now(timezone.utc).isoformat(),
            "writer_pid": os.getpid(), "sqlite_version": sqlite3.sqlite_version,
            "state": state, "database": str(database.resolve()), "operation": operation,
            "identity": dict(identity), "attempts": attempts,
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "attempt_started_elapsed_seconds": round(attempt_started_elapsed, 6),
            "policy": asdict(policy),
            "sqlite_errorcode": getattr(last_error, "sqlite_errorcode", None),
            "sqlite_errorname": getattr(last_error, "sqlite_errorname", None),
            "error": str(last_error) if last_error else None,
        }

    def publish(state: str) -> dict[str, Any]:
        value = diagnostic(state)
        if notify is not None:
            notify(value)
        return value

    while True:
        remaining = deadline - time.monotonic()
        if attempts and (remaining <= 0 or attempts >= policy.max_attempts):
            raise WriteRetryExhausted(publish("EXHAUSTED")) from last_error
        attempts += 1
        attempt_started_elapsed = policy.total_seconds - remaining
        try:
            result = attempt(min(policy.attempt_seconds, max(0, remaining)))
        except sqlite3.Error as error:
            last_error = error
            code = getattr(error, "sqlite_errorcode", None)
            if not isinstance(code, int) or (code & 0xff) != sqlite3.SQLITE_BUSY:
                publish("FAILED")
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0 or attempts >= policy.max_attempts:
                raise WriteRetryExhausted(publish("EXHAUSTED")) from error
            publish("RETRYING")
            delay = min(policy.maximum_delay_seconds,
                        policy.initial_delay_seconds * (2 ** min(attempts - 1, 10)),
                        max(0, deadline - time.monotonic()))
            if delay:
                time.sleep(delay)
        else:
            if last_error is not None:
                publish("COMMITTED")
            return result
