"""Named session cadences, with immutable legacy interpretation.

No Hot worker is enabled here. Interleaved seconds reserve room for its later
adoption; a session cannot mix the named profiles across Uni/Focus runs.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import MappingProxyType

LEGACY = "legacy-v1"
INTERLEAVED = "interleaved-v1"
CADENCE_PROFILES = MappingProxyType({
    LEGACY: MappingProxyType({"uni": (0, 30), "focus": (5, 20, 35, 50)}),
    INTERLEAVED: MappingProxyType({"uni": (0, 30), "focus": (10, 20, 40, 50)}),
})
RESERVED_HOT_SECONDS = tuple(second for second in range(0, 60, 2) if second % 10)


def cadence_seconds(channel: str, profile: str = LEGACY) -> tuple[int, ...]:
    channel = getattr(channel, "value", channel)
    try:
        return CADENCE_PROFILES[profile][channel]
    except (KeyError, TypeError):
        raise ValueError("Unknown polling cadence profile or channel") from None


def run_profile(configuration: dict) -> str | None:
    """Unlabelled historical runs retain their recorded/legacy schedule."""
    channel = configuration.get("watchlist_kind")
    if channel not in ("uni", "focus"):
        return None
    profile = configuration.get("cadence_profile", LEGACY)
    seconds = configuration.get("poll_seconds")
    expected = cadence_seconds(channel, profile)
    if seconds is not None and (not isinstance(seconds, (list, tuple))
                               or any(type(s) is not int for s in seconds)
                               or tuple(seconds) != expected):
        raise ValueError("Recorded polling seconds differ from the named cadence")
    return profile


def journal_profile(runs) -> str | None:
    profiles = set()
    for run in runs:
        configuration = json.loads(run["configuration_json"])
        if not isinstance(configuration, dict):
            raise ValueError("Invalid polling run configuration")
        profile = run_profile(configuration)
        if profile is not None:
            profiles.add(profile)
    if len(profiles) > 1:
        raise ValueError("Mixed polling cadences in one session; use a fresh session boundary")
    return next(iter(profiles), None)


def check_registration(connection: sqlite3.Connection, configuration: dict) -> None:
    """Called inside the native run-registration write transaction.

    Only new explicitly labelled runs opt into this check. Historical/native
    custom-cadence journals remain readable by their existing audit/replay APIs.
    """
    if "cadence_profile" not in configuration:
        return
    selected = run_profile(configuration)
    if selected is None:
        raise ValueError("Named cadence requires a Uni or Focus channel")
    rows = ({"configuration_json": row[0]} for row in connection.execute(
        "SELECT configuration_json FROM poll_run"))
    recorded = journal_profile(rows)
    if recorded is not None and recorded != selected:
        raise ValueError("Cadence change refused: a different session cadence is already registered")


def check_before_credentials(database: Path, profile: str) -> None:
    """Read-only early refusal; registration repeats the check atomically."""
    cadence_seconds("uni", profile)
    if not database.exists():
        return
    if not database.is_file() or database.is_symlink():
        raise ValueError("Cadence check requires an ordinary daily journal")
    with closing(sqlite3.connect(database.resolve().as_uri()+"?mode=ro", uri=True, timeout=.25)) as c:
        c.execute("PRAGMA query_only=ON")
        recorded = journal_profile({"configuration_json": row[0]} for row in c.execute(
            "SELECT configuration_json FROM poll_run"))
    if recorded is not None and recorded != profile:
        raise ValueError("Cadence change refused before credentials: use the original session cadence")
