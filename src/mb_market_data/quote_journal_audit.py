"""Read-only validation and sizing for a daily quote-observation journal."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from mb_market_data.quote_observation_store import SCHEMA_VERSION


UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class AuditFinding:
    """One condition that prevents a journal from passing its audit."""

    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class RunAudit:
    """Readable provenance for one polling process."""

    run_id: str
    started_at_utc: str
    software_version: str
    host: str | None
    channel: str | None
    configuration: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ChannelAudit:
    """Completeness and result counts for one sampling channel."""

    channel: str
    revision_count: int
    member_rows: int
    acquisition_count: int
    observation_count: int
    requested_symbol_count: int
    status_counts: Mapping[str, int]
    expected_slot_count: int
    missing_slot_ids: tuple[str, ...]
    row_count_mismatch_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QuoteJournalAudit:
    """Complete read-only audit result for one SQLite journal."""

    database_path: Path
    session_date: str
    schema_version: int
    integrity_results: tuple[str, ...]
    table_counts: Mapping[str, int]
    runs: tuple[RunAudit, ...]
    channels: tuple[ChannelAudit, ...]
    storage_bytes: Mapping[str, int]
    evidence_bytes: Mapping[str, int]
    findings: tuple[AuditFinding, ...]

    @property
    def passed(self) -> bool:
        return not self.findings


def _open_read_only(database_path: Path) -> sqlite3.Connection:
    uri = database_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _utc_text(value: datetime) -> str:
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _expected_slots(
    channel: str,
    configurations: tuple[Mapping[str, Any], ...],
    *,
    audited_at: datetime,
) -> dict[str, str]:
    expected: dict[str, str] = {}
    for configuration in configurations:
        if configuration.get("watchlist_kind") != channel:
            continue
        start_text = configuration.get("poll_window_start_et")
        end_text = configuration.get("poll_window_end_et")
        seconds = configuration.get("poll_seconds")
        if not isinstance(start_text, str) or not isinstance(end_text, str):
            continue
        if not isinstance(seconds, list) or not all(
            isinstance(second, int) and not isinstance(second, bool)
            for second in seconds
        ):
            continue

        start = datetime.fromisoformat(start_text)
        end = datetime.fromisoformat(end_text)
        if start.tzinfo is None or end.tzinfo is None:
            continue
        through = min(end, audited_at.astimezone(end.tzinfo))
        minute = start.replace(second=0, microsecond=0)
        while minute <= through:
            for second in seconds:
                candidate = minute.replace(second=second)
                if start <= candidate < end and candidate <= through:
                    expected[_utc_text(candidate)] = (
                        f"{channel}:{candidate.isoformat(timespec='seconds')}"
                    )
            minute += timedelta(minutes=1)
    return expected


def _storage_sizes(database_path: Path) -> dict[str, int]:
    paths = {
        "database": database_path,
        "wal": Path(f"{database_path}-wal"),
        "shm": Path(f"{database_path}-shm"),
    }
    return {
        name: path.stat().st_size if path.exists() else 0
        for name, path in paths.items()
    }


def _evidence_sizes(runs: tuple[RunAudit, ...]) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for run in runs:
        value = run.configuration.get("evidence_run_directory")
        if not isinstance(value, str) or not value:
            continue
        path = Path(value)
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.is_dir():
            continue
        sizes[run.run_id] = sum(
            item.stat().st_size for item in path.iterdir() if item.is_file()
        )
    return sizes


def audit_quote_journal(
    database_path: str | Path,
    *,
    audited_at: datetime | None = None,
) -> QuoteJournalAudit:
    """Audit a journal without modifying or checkpointing it.

    Configured polling slots are expected only through ``audited_at``.  This
    makes the same command useful during a live session and after the close.
    """

    path = Path(database_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    observed_at = audited_at or datetime.now(UTC)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("audited_at must be timezone-aware")

    connection = _open_read_only(path)
    try:
        integrity_results = tuple(
            row[0] for row in connection.execute("PRAGMA integrity_check")
        )
        schema_version = connection.execute(
            "PRAGMA user_version"
        ).fetchone()[0]
        identity = connection.execute(
            "SELECT schema_version, session_date FROM store_identity "
            "WHERE singleton = 1"
        ).fetchone()
        if identity is None:
            raise ValueError("Journal has no store_identity row")
        session_date = identity["session_date"]

        table_names = (
            "poll_run",
            "sampling_channel_revision",
            "sampling_channel_member",
            "quote_acquisition",
            "quote_observation",
        )
        table_counts = {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in table_names
        }

        run_rows = connection.execute(
            "SELECT * FROM poll_run ORDER BY started_at_utc, run_id"
        ).fetchall()
        runs = tuple(
            RunAudit(
                run_id=row["run_id"],
                started_at_utc=row["started_at_utc"],
                software_version=row["software_version"],
                host=row["host"],
                channel=(
                    configuration.get("watchlist_kind")
                    if isinstance(
                        configuration := json.loads(
                            row["configuration_json"]
                        ),
                        dict,
                    )
                    else None
                ),
                configuration=(
                    configuration if isinstance(configuration, dict) else {}
                ),
            )
            for row in run_rows
        )

        channels = tuple(
            row[0]
            for row in connection.execute(
                """
                SELECT channel FROM sampling_channel_revision
                UNION
                SELECT channel FROM quote_acquisition
                ORDER BY channel
                """
            )
        )
        channel_audits: list[ChannelAudit] = []
        findings: list[AuditFinding] = []

        if integrity_results != ("ok",):
            findings.append(
                AuditFinding(
                    "sqlite_integrity",
                    "; ".join(integrity_results),
                )
            )
        if schema_version != SCHEMA_VERSION:
            findings.append(
                AuditFinding(
                    "schema_version",
                    f"PRAGMA user_version is {schema_version}; expected "
                    f"{SCHEMA_VERSION}",
                )
            )
        if identity["schema_version"] != schema_version:
            findings.append(
                AuditFinding(
                    "schema_identity",
                    "store_identity schema_version differs from "
                    "PRAGMA user_version",
                )
            )
        foreign_key_violations = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if foreign_key_violations:
            findings.append(
                AuditFinding(
                    "foreign_keys",
                    f"{len(foreign_key_violations)} foreign-key "
                    "violations found",
                )
            )

        revision_mismatches = connection.execute(
            """
            SELECT revision.channel, revision.revision,
                   revision.symbol_count, COUNT(member.symbol) AS actual
            FROM sampling_channel_revision AS revision
            LEFT JOIN sampling_channel_member AS member
              ON member.channel = revision.channel
             AND member.session_date = revision.session_date
             AND member.revision = revision.revision
            GROUP BY revision.channel, revision.session_date,
                     revision.revision, revision.symbol_count
            HAVING actual != revision.symbol_count
            """
        ).fetchall()
        for row in revision_mismatches:
            findings.append(
                AuditFinding(
                    "revision_member_count",
                    f"{row['channel']} r{row['revision']}: declared "
                    f"{row['symbol_count']}, found {row['actual']}",
                )
            )

        configurations = tuple(run.configuration for run in runs)
        for channel in channels:
            revision_count = connection.execute(
                "SELECT COUNT(*) FROM sampling_channel_revision "
                "WHERE channel = ?",
                (channel,),
            ).fetchone()[0]
            member_rows = connection.execute(
                "SELECT COUNT(*) FROM sampling_channel_member "
                "WHERE channel = ?",
                (channel,),
            ).fetchone()[0]
            acquisition_rows = connection.execute(
                """
                SELECT acquisition.acquisition_id,
                       acquisition.scheduled_at_utc,
                       acquisition.requested_symbol_count,
                       COUNT(observation.symbol) AS actual_rows
                FROM quote_acquisition AS acquisition
                LEFT JOIN quote_observation AS observation
                  ON observation.acquisition_id = acquisition.acquisition_id
                WHERE acquisition.channel = ?
                GROUP BY acquisition.acquisition_id
                ORDER BY acquisition.scheduled_at_utc
                """,
                (channel,),
            ).fetchall()
            mismatch_ids = tuple(
                row["acquisition_id"]
                for row in acquisition_rows
                if row["requested_symbol_count"] != row["actual_rows"]
            )
            for acquisition_id in mismatch_ids:
                findings.append(
                    AuditFinding(
                        "acquisition_observation_count",
                        f"{acquisition_id} does not contain one row per "
                        "requested symbol",
                    )
                )

            status_counts = Counter(
                {
                    row["status"]: row["count"]
                    for row in connection.execute(
                        """
                        SELECT observation.status, COUNT(*) AS count
                        FROM quote_observation AS observation
                        JOIN quote_acquisition AS acquisition
                          ON acquisition.acquisition_id =
                             observation.acquisition_id
                        WHERE acquisition.channel = ?
                        GROUP BY observation.status
                        """,
                        (channel,),
                    )
                }
            )
            recorded_slots = {
                row["scheduled_at_utc"] for row in acquisition_rows
            }
            expected_slots = _expected_slots(
                channel,
                configurations,
                audited_at=observed_at,
            )
            missing_timestamps = sorted(
                set(expected_slots) - recorded_slots
            )
            missing_slot_ids = tuple(
                expected_slots[value]
                for value in missing_timestamps
            )
            if missing_slot_ids:
                findings.append(
                    AuditFinding(
                        "missing_poll_slots",
                        f"{channel}: {len(missing_slot_ids)} configured "
                        "slots have no acquisition",
                    )
                )

            channel_audits.append(
                ChannelAudit(
                    channel=channel,
                    revision_count=revision_count,
                    member_rows=member_rows,
                    acquisition_count=len(acquisition_rows),
                    observation_count=sum(status_counts.values()),
                    requested_symbol_count=sum(
                        row["requested_symbol_count"]
                        for row in acquisition_rows
                    ),
                    status_counts=dict(sorted(status_counts.items())),
                    expected_slot_count=len(expected_slots),
                    missing_slot_ids=missing_slot_ids,
                    row_count_mismatch_ids=mismatch_ids,
                )
            )
    finally:
        connection.close()

    return QuoteJournalAudit(
        database_path=path,
        session_date=session_date,
        schema_version=schema_version,
        integrity_results=integrity_results,
        table_counts=table_counts,
        runs=runs,
        channels=tuple(channel_audits),
        storage_bytes=_storage_sizes(path),
        evidence_bytes=_evidence_sizes(runs),
        findings=tuple(findings),
    )
