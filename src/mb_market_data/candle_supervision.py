"""Quiet supervision of recoverable candle input; no quote-poller controls."""
from __future__ import annotations

import errno
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

UTC = timezone.utc
HEARTBEAT_GRACE = 10


def stamp(value=None):
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="microseconds")


def instant(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("An acquisition/health timestamp must include its timezone")
    return result.astimezone(UTC)


def publish_json(path, value, *, replace=False, attempts=5):
    """Publish complete, flushed JSON. Immutable evidence never gets replaced.

    A same-directory hard link gives create-if-absent publication on Windows
    and POSIX. Temporary names are outside the reader's receipt pattern.
    """
    path = Path(path)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    body = (json.dumps(value, indent=2, allow_nan=False) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as out:
            out.write(body)
            out.flush()
            os.fsync(out.fileno())
        for attempt in range(attempts):
            try:
                if replace:
                    os.replace(temporary, path)
                else:
                    os.link(temporary, path)
                break
            except OSError as exc:
                sharing_error = (getattr(exc, "winerror", None) in (5, 32, 33)
                                 or exc.errno in (errno.EACCES, errno.EBUSY))
                if not sharing_error or attempt == attempts - 1:
                    raise
                time.sleep(.02 * (attempt + 1))
    finally:
        # A sharing denial while removing an already published temporary name
        # must not turn a successful commit into a false acquisition failure.
        # Readers ignore these names; cleanup can safely be retried later.
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class DirectoryLock:
    """An OS-held lock releases on process death; its file is not ownership."""
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.file = self.path.open("a+b")
        self.file.seek(0, os.SEEK_END)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise ValueError("A candle process already holds " + self.path.name) from None
        return self

    def __exit__(self, *args):
        if self.file is not None:
            self.file.close()
            self.file = None


class CandleHealth:
    """Reduce validated acquisitions and lifecycle into quiet current health.

    Incidents describe input availability. Restoration does not assert that
    every absent market interval has been backfilled or provider-finalized.
    """
    def __init__(self, root, plan):
        self.root = Path(root)
        self.plan = plan
        self.incident = None
        self.events = []
        self.pending_events = []
        self.last_success = None
        self.last_receipt = None
        self._seen = set()
        self._active_run = None
        self._run_success = False
        self._latest_outcome = None
        self.publication_error = None

    def _event(self, kind, now, **details):
        value = {"kind": kind, "observed_at_utc": stamp(now),
                 "session_id": self.plan["session_id"], "symbol": self.plan["symbol"],
                 **details}
        self.events.append(value)
        self.pending_events.append((uuid.uuid4().hex + ".json", value))

    def _flush_events(self):
        folder = self.root / "health_events"
        folder.mkdir(exist_ok=True)
        while self.pending_events:
            name, event = self.pending_events[0]
            publish_json(folder / name, event)
            self.pending_events.pop(0)

    def inspect(self, *, run_id, phase, alive, reader, now, reader_error=None):
        if run_id != self._active_run:
            self._active_run, self._run_success = run_id, False
            self._latest_outcome = None
        new_success = None
        if reader is not None:
            for item in reader.receipt_summaries:
                key = (run_id, item.sample_number)
                if key in self._seen:
                    continue
                self._seen.add(key)
                self.last_receipt = item.received_at_utc
                self._latest_outcome = item.outcome
                if item.outcome == "OK":
                    self.last_success = new_success = item.received_at_utc
                    self._run_success = True
        reason = None
        stage = phase.get("state", "STARTING")
        expected = self.plan["interval_seconds"] + self.plan["http_timeout_seconds"] + 10
        if reader_error:
            reason = "Committed candle evidence could not be validated"
        elif stage in {"STOPPED_AFTER_ERRORS", "FAILED"}:
            reason = "Candle producer failed; captured evidence is retained"
        elif not alive and stage not in {"DURATION_COMPLETE", "STOPPED_BY_OPERATOR", "WINDOW_ENDED"}:
            reason = "Candle producer exited unexpectedly"
        elif self._latest_outcome is not None and self._latest_outcome != "OK":
            reason = "Candle acquisition error: " + self._latest_outcome
        elif stage not in {"WAITING_FOR_CREDENTIALS", "DURATION_COMPLETE", "STOPPED_BY_OPERATOR", "WINDOW_ENDED"}:
            anchor = self.last_success if self._run_success else instant(phase["started_at_utc"])
            if (now - anchor).total_seconds() > expected:
                reason = "No new successful candle acquisition within the expected request interval"
        if reason and self.incident is None:
            self.incident = {"incident_id": uuid.uuid4().hex, "detected_at_utc": stamp(now),
                             "last_successful_acquisition_at_utc": stamp(self.last_success) if self.last_success else None,
                             "reason": reason, "run_id": run_id}
            self._event("INPUT_INTERRUPTED", now, **self.incident)
        # A valid replacement response must be newer than the detected incident.
        if (not reason and self.incident is not None and new_success is not None
                and new_success >= instant(self.incident["detected_at_utc"])):
            self._event("INPUT_RESTORED", now, incident_id=self.incident["incident_id"],
                        restored_by_run_id=run_id, recovery_acquisition_at_utc=stamp(new_success),
                        previous_successful_acquisition_at_utc=self.incident["last_successful_acquisition_at_utc"],
                        backfill_status="NEEDS_REVIEW",
                        note="Input flow restored; absent intervals and corrections still require review")
            self.incident = None
        state = stage
        if reason:
            state = "WARNING"
        elif self.incident is not None:
            state = "RECOVERY_PENDING"
            reason = "Awaiting a successful new acquisition; earlier interruption remains recorded"
        elif alive and stage == "RUNNING" and self._run_success:
            state = "HEALTHY"
        elif stage == "RUNNING":
            state = "AWAITING_FIRST_RESPONSE"
        try:
            self._flush_events()
            self.publication_error = None
        except OSError as exc:
            self.publication_error = type(exc).__name__
            state, reason = "WARNING", "Candle incident publication failed; monitoring continues"
        return {"schema_version": 1, "session_id": self.plan["session_id"],
                "symbol": self.plan["symbol"], "session_date": self.plan["session_date"],
                "state": state, "severity": "warning" if reason else "notice",
                "message": reason or {
                    "HEALTHY": "Candle acquisitions are progressing",
                    "WAITING_FOR_CREDENTIALS": "Waiting for credentials in the candle-producer console",
                    "DURATION_COMPLETE": "Bounded candle capture complete",
                    "STOPPED_BY_OPERATOR": "Candle capture stopped by the operator",
                    "WINDOW_ENDED": "Candle capture reached the regular-session end",
                }.get(state, "Awaiting a successful candle response"),
                "heartbeat_at_utc": stamp(now), "active_run": run_id,
                "last_acquisition_at_utc": stamp(self.last_receipt) if self.last_receipt else None,
                "last_successful_acquisition_at_utc": stamp(self.last_success) if self.last_success else None,
                "open_incident": self.incident,
                "recorded_interruptions": sum(e["kind"] == "INPUT_INTERRUPTED" for e in self.events),
                "backfill_reviews_pending": sum(e["kind"] == "INPUT_RESTORED" for e in self.events),
                "audible_alarm": False}


def health_view(root, *, now=None):
    """Independent health reader; freshness uses wall time, never replay time."""
    now = now or datetime.now(UTC)
    root = Path(root)
    base = {"state": "WARNING", "severity": "warning", "audible_alarm": False}
    try:
        plan = json.loads((root / "session_plan.json").read_text(encoding="utf-8"))
        status = json.loads((root / "supervisor_status.json").read_text(encoding="utf-8"))
        if status["session_id"] != plan["session_id"]:
            raise ValueError("Session identity differs")
        if status.get("state") == "SESSION_CLOSED":
            return status
        age = (now - instant(status["heartbeat_at_utc"])).total_seconds()
        if age < -2 or age > HEARTBEAT_GRACE:
            return {**status, **base, "message": "Candle supervisor heartbeat is stale; candle health is unknown"}
        watcher = json.loads((root / "watchdog_status.json").read_text(encoding="utf-8"))
        watcher_age = (now - instant(watcher["heartbeat_at_utc"])).total_seconds()
        if watcher["session_id"] != plan["session_id"] or watcher_age < -2 or watcher_age > HEARTBEAT_GRACE:
            return {**status, **base, "message": "Candle watchdog heartbeat is stale; independent supervision is unavailable"}
        return status
    except (OSError, ValueError, KeyError, TypeError):
        return {**base, "message": "Candle health status is unavailable; acquired evidence remains separate"}


def health_text(status):
    if status is None:
        return "", "candle-health-panel"
    bits = ["CANDLE " + status.get("symbol", ""), status["state"].replace("_", " "), status["message"]]
    if status.get("last_successful_acquisition_at_utc"):
        received = instant(status["last_successful_acquisition_at_utc"]).astimezone(ZoneInfo("America/New_York"))
        bits.append("Last successful acquisition: " + received.strftime("%Y-%m-%d %H:%M:%S %Z"))
    if status.get("backfill_reviews_pending"):
        bits.append(f"Recovered interruptions awaiting backfill review: {status['backfill_reviews_pending']}")
    bits.append("Current source health; independent of viewing time")
    return " · ".join(bits), "candle-health-panel candle-health-" + status.get("severity", "notice")
