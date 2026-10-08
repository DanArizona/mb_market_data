"""Read operational health files without opening or changing a journal."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


def read_health_status(directory: Path, *, journal: Path, session_date: date,
                       now: datetime | None = None, maximum_age_seconds: float = 30) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    result = {"state": "UNAVAILABLE", "reason": "No current health report is available.",
              "observed_at_utc": None, "age_seconds": None, "channels": {}, "restart_launchers": {}}
    try:
        path = directory / "latest.json"
        if path.stat().st_size > 1_000_000:
            raise ValueError("Health report exceeds its size limit")
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if (snapshot["session_date"] != str(session_date)
                or Path(snapshot["journal"]).resolve() != journal.resolve()):
            raise ValueError("Health report belongs to a different session or journal")
        observed = datetime.fromisoformat(snapshot["observed_at_utc"].replace("Z", "+00:00"))
        if observed.tzinfo is None or not isinstance(snapshot["channels"], dict):
            raise ValueError("Health report has invalid timing or channels")
        for info in snapshot["channels"].values():
            if not isinstance(info, dict) or not isinstance(info.get("state"), str):
                raise ValueError("Health report has an invalid channel state")
            committed = info.get("last_committed_slot_utc")
            if committed is not None and datetime.fromisoformat(committed.replace("Z", "+00:00")).tzinfo is None:
                raise ValueError("Health report has an invalid commit time")
        age = (now - observed).total_seconds()
        if age < -2:
            raise ValueError("Health report is dated in the future")
        result.update(observed_at_utc=observed.isoformat(), age_seconds=max(0, age),
                      channels=snapshot["channels"], restart_launchers=snapshot.get("restart_launchers", {}),
                      journal_read=snapshot.get("journal_read"), journal_error=snapshot.get("journal_error"),
                      process_inspection=snapshot.get("process_inspection", {}))
        result.update(state="CURRENT" if age <= maximum_age_seconds else "STALE",
                      reason="Recent monitor check." if age <= maximum_age_seconds else "Monitor report is stale; displayed channel values are the last known state.")
        supervisor = directory / "supervisor.json"
        if supervisor.exists():
            watched = json.loads(supervisor.read_text(encoding="utf-8"))
            watched_at = datetime.fromisoformat(watched["observed_at_utc"].replace("Z", "+00:00"))
            if (watched.get("monitor_token") == snapshot.get("monitor_token")
                    and watched.get("child_pid") == snapshot.get("monitor_pid")
                    and Path(watched["journal"]).resolve() == journal.resolve()):
                if (now - watched_at).total_seconds() > maximum_age_seconds:
                    result.update(state="STALE", reason="Monitor watchdog report is stale; current monitoring is not confirmed.")
                elif watched["state"] in {"STOPPED", "OVERDUE"}:
                    result.update(state="STOPPED" if watched["state"] == "STOPPED" else "STALE", reason=watched["detail"])
        stop = directory / "last_stop.json"
        if stop.exists():
            receipt = json.loads(stop.read_text(encoding="utf-8"))
            stopped = datetime.fromisoformat(receipt["stopped_at_utc"].replace("Z", "+00:00"))
            if (Path(receipt["journal"]).resolve() == journal.resolve() and stopped >= observed
                    and receipt.get("monitor_pid") == snapshot.get("monitor_pid")):
                result.update(state="STOPPED", reason="Monitor stopped; displayed channel values are the last known state.")
        return result
    except (OSError, ValueError, TypeError, KeyError) as error:
        result.update(state="UNAVAILABLE", reason=str(error), channels={}, restart_launchers={})
        return result
