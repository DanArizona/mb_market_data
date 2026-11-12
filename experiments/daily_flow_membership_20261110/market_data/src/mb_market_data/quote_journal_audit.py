"""Read-only validation and sizing for a daily quote-observation journal."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from mb_market_data.quote_observation_store import (
    HIERARCHY_SCHEMA_VERSION,
    SUPPORTED_STORE_SCHEMA_VERSIONS,
    QuoteObservationStore,
    QuoteObservationStoreError,
)
from mb_market_data.sampling_membership import SAMPLING_HIERARCHY_CONTRACT


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
    skipped_slot_count: int
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
    membership_contract: str
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


def _fingerprint(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
        membership_contract = "legacy-independent-channels"
        if schema_version == HIERARCHY_SCHEMA_VERSION:
            contract_row = connection.execute(
                "SELECT membership_contract FROM store_identity "
                "WHERE singleton = 1"
            ).fetchone()
            membership_contract = (
                contract_row["membership_contract"]
                if contract_row is not None
                else "missing"
            )

        table_names = [
            "poll_run",
            "sampling_channel_revision",
            "sampling_channel_member",
            "quote_acquisition",
            "quote_observation",
        ]
        skip_table_exists = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'poll_slot_skip'"
            ).fetchone()
            is not None
        )
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'sampling_membership_revision'"
        ).fetchone():
            table_names.insert(1, "sampling_membership_revision")
        if skip_table_exists:
            table_names.insert(-2, "poll_slot_skip")
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
                (
                    "SELECT channel FROM sampling_channel_revision "
                    "UNION SELECT channel FROM quote_acquisition "
                    + (
                        "UNION SELECT channel FROM poll_slot_skip "
                        if skip_table_exists
                        else ""
                    )
                    + "ORDER BY channel"
                )
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
        if schema_version not in SUPPORTED_STORE_SCHEMA_VERSIONS:
            findings.append(
                AuditFinding(
                    "schema_version",
                    f"PRAGMA user_version is {schema_version}; supported "
                    f"versions are {sorted(SUPPORTED_STORE_SCHEMA_VERSIONS)}",
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
        if (
            schema_version == HIERARCHY_SCHEMA_VERSION
            and membership_contract != SAMPLING_HIERARCHY_CONTRACT
        ):
            findings.append(
                AuditFinding(
                    "membership_contract",
                    "store_identity membership contract is "
                    f"{membership_contract!r}; expected "
                    f"{SAMPLING_HIERARCHY_CONTRACT!r}",
                )
            )
        if (
            schema_version == HIERARCHY_SCHEMA_VERSION
            and not skip_table_exists
        ):
            findings.append(
                AuditFinding(
                    "poll_slot_skip_table",
                    "schema-v2 journal has no durable skipped-slot table",
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

        if schema_version == HIERARCHY_SCHEMA_VERSION:
            hierarchy_rows = connection.execute(
                """
                SELECT revision, effective_at_utc
                FROM sampling_membership_revision
                WHERE session_date = ?
                ORDER BY revision
                """,
                (session_date,),
            ).fetchall()
            if not hierarchy_rows:
                findings.append(
                    AuditFinding(
                        "hierarchy_missing",
                        "schema-v2 journal has no membership revision",
                    )
                )
            revision_numbers = tuple(row["revision"] for row in hierarchy_rows)
            expected_revisions = tuple(range(len(hierarchy_rows)))
            if revision_numbers != expected_revisions:
                findings.append(
                    AuditFinding(
                        "hierarchy_revision_sequence",
                        f"found revisions {revision_numbers}; expected "
                        f"{expected_revisions}",
                    )
                )
            effective_times = tuple(
                row["effective_at_utc"] for row in hierarchy_rows
            )
            if effective_times != tuple(sorted(effective_times)):
                findings.append(
                    AuditFinding(
                        "hierarchy_effective_order",
                        "hierarchy effective times are not monotonic",
                    )
                )

            expected_channels = {"uni", "focus", "hot"}
            for row in hierarchy_rows:
                channel_rows = connection.execute(
                    """
                    SELECT channel, effective_at_utc
                    FROM sampling_channel_revision
                    WHERE session_date = ? AND revision = ?
                    """,
                    (session_date, row["revision"]),
                ).fetchall()
                actual_channels = {
                    item["channel"] for item in channel_rows
                }
                if (
                    actual_channels != expected_channels
                    or len(channel_rows) != len(expected_channels)
                ):
                    findings.append(
                        AuditFinding(
                            "hierarchy_bundle_channels",
                            f"r{row['revision']} contains channels "
                            f"{tuple(sorted(actual_channels))}; expected "
                            "('focus', 'hot', 'uni')",
                        )
                    )
                if any(
                    item["effective_at_utc"] != row["effective_at_utc"]
                    for item in channel_rows
                ):
                    findings.append(
                        AuditFinding(
                            "hierarchy_bundle_effective_time",
                            f"r{row['revision']} channel effective times "
                            "differ from the hierarchy header",
                        )
                    )

            try:
                hierarchy_store = QuoteObservationStore(
                    path,
                    session_date=date.fromisoformat(session_date),
                    schema_version=HIERARCHY_SCHEMA_VERSION,
                )
                hierarchy_store.membership_revisions_in_effective_order()
            except (QuoteObservationStoreError, TypeError, ValueError) as exc:
                findings.append(
                    AuditFinding(
                        "hierarchy_content",
                        f"{type(exc).__name__}: {exc}",
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
            skip_rows = (
                connection.execute(
                    """
                    SELECT *
                    FROM poll_slot_skip
                    WHERE channel = ?
                    ORDER BY scheduled_at_utc, slot_id
                    """,
                    (channel,),
                ).fetchall()
                if skip_table_exists
                else []
            )
            for skipped in skip_rows:
                header = (
                    skipped["slot_id"],
                    skipped["run_id"],
                    skipped["channel"],
                    skipped["channel_revision"],
                    skipped["session_date"],
                    skipped["scheduled_at_utc"],
                    skipped["membership_effective_at_utc"],
                    skipped["observed_at_utc"],
                    skipped["reason"],
                )
                if skipped["content_sha256"] != _fingerprint(header):
                    findings.append(
                        AuditFinding(
                            "skip_content_hash",
                            f"{skipped['slot_id']} content hash differs "
                            "from its stored values",
                        )
                    )

                bound_revision = connection.execute(
                    """
                    SELECT effective_at_utc, symbol_count
                    FROM sampling_channel_revision
                    WHERE channel = ? AND session_date = ?
                      AND revision = ?
                    """,
                    (
                        channel,
                        skipped["session_date"],
                        skipped["channel_revision"],
                    ),
                ).fetchone()
                latest_revision = connection.execute(
                    """
                    SELECT revision
                    FROM sampling_membership_revision
                    WHERE session_date = ? AND effective_at_utc <= ?
                    ORDER BY effective_at_utc DESC, revision DESC
                    LIMIT 1
                    """,
                    (
                        skipped["session_date"],
                        skipped["scheduled_at_utc"],
                    ),
                ).fetchone()
                if (
                    bound_revision is None
                    or bound_revision["effective_at_utc"]
                    != skipped["membership_effective_at_utc"]
                    or latest_revision is None
                    or latest_revision["revision"]
                    != skipped["channel_revision"]
                ):
                    findings.append(
                        AuditFinding(
                            "skip_revision_binding",
                            f"{skipped['slot_id']} is not bound to the "
                            "latest hierarchy revision effective at its slot",
                        )
                    )
                if (
                    bound_revision is not None
                    and bound_revision["symbol_count"] != 0
                ):
                    findings.append(
                        AuditFinding(
                            "skip_nonempty_membership",
                            f"{skipped['slot_id']} skips a nonempty "
                            f"{channel} membership",
                        )
                    )
                if connection.execute(
                    "SELECT 1 FROM quote_acquisition WHERE slot_id = ?",
                    (skipped["slot_id"],),
                ).fetchone():
                    findings.append(
                        AuditFinding(
                            "slot_outcome_conflict",
                            f"{skipped['slot_id']} has both an acquisition "
                            "and a skipped-slot record",
                        )
                    )
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

            if schema_version == HIERARCHY_SCHEMA_VERSION:
                binding_rows = connection.execute(
                    """
                    SELECT acquisition.acquisition_id,
                           acquisition.channel_revision,
                           acquisition.scheduled_at_utc,
                           revision.effective_at_utc
                    FROM quote_acquisition AS acquisition
                    LEFT JOIN sampling_channel_revision AS revision
                      ON revision.channel = acquisition.channel
                     AND revision.session_date = acquisition.session_date
                     AND revision.revision = acquisition.channel_revision
                    WHERE acquisition.channel = ?
                    ORDER BY acquisition.scheduled_at_utc,
                             acquisition.acquisition_id
                    """,
                    (channel,),
                ).fetchall()
                for binding in binding_rows:
                    if (
                        binding["effective_at_utc"] is not None
                        and binding["effective_at_utc"]
                        > binding["scheduled_at_utc"]
                    ):
                        findings.append(
                            AuditFinding(
                                "acquisition_revision_effective_time",
                                f"{binding['acquisition_id']} uses "
                                f"{channel} r{binding['channel_revision']} "
                                "before that revision is effective",
                            )
                        )
                    latest_revision = connection.execute(
                        """
                        SELECT revision
                        FROM sampling_membership_revision
                        WHERE session_date = ? AND effective_at_utc <= ?
                        ORDER BY effective_at_utc DESC, revision DESC
                        LIMIT 1
                        """,
                        (session_date, binding["scheduled_at_utc"]),
                    ).fetchone()
                    if (
                        latest_revision is None
                        or latest_revision["revision"]
                        != binding["channel_revision"]
                    ):
                        findings.append(
                            AuditFinding(
                                "acquisition_latest_revision",
                                f"{binding['acquisition_id']} does not use "
                                "the latest hierarchy revision effective "
                                "at its scheduled slot",
                            )
                        )
                    observed_symbols = tuple(
                        row["symbol"]
                        for row in connection.execute(
                            """
                            SELECT symbol
                            FROM quote_observation
                            WHERE acquisition_id = ?
                            ORDER BY ordinal
                            """,
                            (binding["acquisition_id"],),
                        )
                    )
                    revision_symbols = tuple(
                        row["symbol"]
                        for row in connection.execute(
                            """
                            SELECT symbol
                            FROM sampling_channel_member
                            WHERE channel = ? AND session_date = ?
                              AND revision = ?
                            ORDER BY ordinal
                            """,
                            (
                                channel,
                                session_date,
                                binding["channel_revision"],
                            ),
                        )
                    )
                    if observed_symbols != revision_symbols:
                        findings.append(
                            AuditFinding(
                                "acquisition_revision_binding",
                                f"{binding['acquisition_id']} symbols do "
                                f"not match {channel} "
                                f"r{binding['channel_revision']}",
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
            recorded_slots.update(
                row["scheduled_at_utc"] for row in skip_rows
            )
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
                        "slots have neither an acquisition nor an "
                        "empty-membership skip",
                    )
                )

            channel_audits.append(
                ChannelAudit(
                    channel=channel,
                    revision_count=revision_count,
                    member_rows=member_rows,
                    acquisition_count=len(acquisition_rows),
                    skipped_slot_count=len(skip_rows),
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
        membership_contract=membership_contract,
        integrity_results=integrity_results,
        table_counts=table_counts,
        runs=runs,
        channels=tuple(channel_audits),
        storage_bytes=_storage_sizes(path),
        evidence_bytes=_evidence_sizes(runs),
        findings=tuple(findings),
    )
