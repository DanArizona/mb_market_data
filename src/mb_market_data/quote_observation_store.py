"""Daily SQLite journal for scheduled Schwab quote observations.

The journal is the high-resolution input for exact day replay. It stores
polling-run provenance, immutable sampling-channel revisions, acquisition
timing, and one normalized result for every requested symbol.

Channel names are data rather than schema. Adding ``hot`` or another future
channel therefore does not require a database migration. Full Schwab payloads
belong in the separate raw-evidence stream; this journal retains the typed,
queryable values required by live decisions and replay.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator, Mapping

from mb_market_data.sampling_membership import (
    SAMPLING_HIERARCHY_CONTRACT,
    RevisionTransition,
    SamplingChannel,
    SamplingHierarchyRevision,
    assess_revision_candidate,
)
from mb_market_data.schwab_quotes import (
    QuoteBatchResult,
    QuoteResult,
    normalize_symbols,
)
from mb_market_data.watchlist_polling import PollRequest, SkippedPollSlot


SCHEMA_VERSION = 1
HIERARCHY_SCHEMA_VERSION = 2
SUPPORTED_STORE_SCHEMA_VERSIONS = frozenset(
    {SCHEMA_VERSION, HIERARCHY_SCHEMA_VERSION}
)


class QuoteObservationStoreError(RuntimeError):
    """Base error for quote-observation persistence."""


class DuplicateRecordError(QuoteObservationStoreError):
    """An immutable ID already exists with different content."""


class UnknownRunError(QuoteObservationStoreError):
    """An acquisition refers to an unrecorded polling run."""


class UnknownChannelRevisionError(QuoteObservationStoreError):
    """An acquisition refers to an unrecorded channel revision."""


class SessionMismatchError(QuoteObservationStoreError):
    """A record or caller uses the wrong daily session."""


class UnsupportedSchemaVersionError(QuoteObservationStoreError):
    """The database schema is newer or otherwise unsupported."""


class RecordResult(StrEnum):
    """Outcome of recording one immutable journal item."""

    INSERTED = "inserted"
    ALREADY_PRESENT = "already_present"


@dataclass(frozen=True, slots=True)
class PollRunProvenance:
    """Software and configuration identity for one polling process."""

    run_id: str
    started_at: datetime
    software_version: str
    configuration: Mapping[str, Any]
    host: str | None = None
    command: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_nonblank(self.run_id, "run_id")
        _require_aware(self.started_at, "started_at")
        _require_nonblank(self.software_version, "software_version")


@dataclass(frozen=True, slots=True)
class SamplingChannelRevision:
    """Immutable membership for a named sampling channel."""

    channel: str
    session_date: date
    revision: int
    effective_at: datetime
    symbols: tuple[str, ...]
    source: str
    reason: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_nonblank(self.channel, "channel")
        _require_aware(self.effective_at, "effective_at")
        _require_nonblank(self.source, "source")
        if isinstance(self.revision, bool) or not isinstance(
            self.revision, int
        ):
            raise TypeError("revision must be an integer")
        if self.revision < 0:
            raise ValueError("revision must be nonnegative")

        normalized = normalize_symbols(self.symbols)
        if not normalized:
            raise ValueError("symbols must contain at least one symbol")
        object.__setattr__(self, "channel", self.channel.strip().casefold())
        object.__setattr__(self, "symbols", normalized)


@dataclass(frozen=True, slots=True)
class StoredAcquisition:
    """Persisted acquisition header without its per-symbol rows."""

    acquisition_id: str
    run_id: str
    channel: str
    channel_revision: int
    session_date: date
    slot_id: str
    scheduled_at_utc: datetime
    dispatched_at_utc: datetime
    completed_at_utc: datetime
    request_count: int
    batch_size: int
    requested_symbol_count: int
    unexpected_symbols: tuple[str, ...]
    content_sha256: str


@dataclass(frozen=True, slots=True)
class StoredQuoteObservation:
    """One normalized symbol result with replay provenance."""

    acquisition_id: str
    channel: str
    channel_revision: int
    scheduled_at_utc: datetime
    symbol: str
    ordinal: int
    status: str
    detail: str | None
    schwab_batch_number: int
    request_started_at_utc: datetime
    response_received_at_utc: datetime | None
    values: Mapping[str, Any]


OBSERVATION_COLUMNS: tuple[str, ...] = (
    "asset_main_type",
    "asset_sub_type",
    "realtime",
    "extended_bid_price",
    "extended_ask_price",
    "extended_mark",
    "extended_last_price",
    "extended_last_size",
    "extended_total_volume",
    "extended_quote_time_ms",
    "extended_trade_time_ms",
    "quote_bid_price",
    "quote_ask_price",
    "quote_mark",
    "quote_last_price",
    "quote_bid_size",
    "quote_ask_size",
    "quote_last_size",
    "quote_total_volume",
    "quote_security_status",
    "quote_open_price",
    "quote_high_price",
    "quote_low_price",
    "quote_close_price",
    "quote_net_change",
    "quote_net_percent_change",
    "quote_time_ms",
    "quote_trade_time_ms",
    "quote_bid_time_ms",
    "quote_ask_time_ms",
    "post_market_change",
    "post_market_percent_change",
    "regular_market_last_price",
    "regular_market_last_size",
    "regular_market_trade_time_ms",
    "regular_market_net_change",
    "regular_market_percent_change",
    "shares_outstanding",
    "avg_10_days_volume",
    "avg_1_year_volume",
    "exchange",
    "exchange_name",
    "description",
)


def _require_nonblank(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _utc_text(value: datetime, name: str) -> str:
    _require_aware(value, name)
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _json_text(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_json_default,
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(
        f"Object of type {type(value).__name__} is not JSON serializable"
    )


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_json_text(value).encode("utf-8")).hexdigest()


def _nested(value: Mapping[str, Any], *path: str) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _sqlite_bool(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    raise TypeError("realtime must be a boolean or None")


def _normalized_quote_values(result: QuoteResult) -> tuple[Any, ...]:
    quote = result.quote or {}
    return (
        quote.get("assetMainType"),
        quote.get("assetSubType"),
        _sqlite_bool(quote.get("realtime")),
        _nested(quote, "extended", "bidPrice"),
        _nested(quote, "extended", "askPrice"),
        _nested(quote, "extended", "mark"),
        _nested(quote, "extended", "lastPrice"),
        _nested(quote, "extended", "lastSize"),
        _nested(quote, "extended", "totalVolume"),
        _nested(quote, "extended", "quoteTime"),
        _nested(quote, "extended", "tradeTime"),
        _nested(quote, "quote", "bidPrice"),
        _nested(quote, "quote", "askPrice"),
        _nested(quote, "quote", "mark"),
        _nested(quote, "quote", "lastPrice"),
        _nested(quote, "quote", "bidSize"),
        _nested(quote, "quote", "askSize"),
        _nested(quote, "quote", "lastSize"),
        _nested(quote, "quote", "totalVolume"),
        _nested(quote, "quote", "securityStatus"),
        _nested(quote, "quote", "openPrice"),
        _nested(quote, "quote", "highPrice"),
        _nested(quote, "quote", "lowPrice"),
        _nested(quote, "quote", "closePrice"),
        _nested(quote, "quote", "netChange"),
        _nested(quote, "quote", "netPercentChange"),
        _nested(quote, "quote", "quoteTime"),
        _nested(quote, "quote", "tradeTime"),
        _nested(quote, "quote", "bidTime"),
        _nested(quote, "quote", "askTime"),
        _nested(quote, "quote", "postMarketChange"),
        _nested(quote, "quote", "postMarketPercentChange"),
        _nested(quote, "regular", "regularMarketLastPrice"),
        _nested(quote, "regular", "regularMarketLastSize"),
        _nested(quote, "regular", "regularMarketTradeTime"),
        _nested(quote, "regular", "regularMarketNetChange"),
        _nested(quote, "regular", "regularMarketPercentChange"),
        _nested(quote, "fundamental", "sharesOutstanding"),
        _nested(quote, "fundamental", "avg10DaysVolume"),
        _nested(quote, "fundamental", "avg1YearVolume"),
        _nested(quote, "reference", "exchange"),
        _nested(quote, "reference", "exchangeName"),
        _nested(quote, "reference", "description"),
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class QuoteObservationStore:
    """Process-safe, file-backed journal bound to one trading session."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        session_date: date,
        schema_version: int = SCHEMA_VERSION,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if isinstance(schema_version, bool) or not isinstance(
            schema_version, int
        ):
            raise TypeError("schema_version must be an integer")
        if schema_version not in SUPPORTED_STORE_SCHEMA_VERSIONS:
            raise UnsupportedSchemaVersionError(
                "Unsupported quote-observation schema version: "
                f"{schema_version}"
            )
        if isinstance(busy_timeout_ms, bool) or not isinstance(
            busy_timeout_ms, int
        ):
            raise TypeError("busy_timeout_ms must be an integer")
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be greater than zero")

        self.database_path = Path(database_path)
        self.session_date = session_date
        self.schema_version = schema_version
        self.busy_timeout_ms = busy_timeout_ms

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_ms / 1_000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            f"PRAGMA busy_timeout = {self.busy_timeout_ms}"
        )
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Commit or roll back, then always release the database handle."""

        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _enable_wal(self, connection: sqlite3.Connection) -> None:
        """Enable WAL, retrying SQLite's transient initialization lock.

        SQLite's configured busy timeout is not consistently honored by
        ``PRAGMA journal_mode`` on Windows.  Two pollers opening a new daily
        journal can therefore race here even though their later writes wait
        normally.  Keep this retry limited to lock/busy errors and bounded by
        the store's existing timeout.
        """

        deadline = time.monotonic() + (self.busy_timeout_ms / 1_000)
        while True:
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as error:
                message = str(error).casefold()
                if "locked" not in message and "busy" not in message:
                    raise

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(0.05, remaining))

    def initialize(self) -> None:
        """Create or validate one explicitly selected daily schema."""

        self.database_path.parent.mkdir(parents=True, exist_ok=True)

        with self._connection() as connection:
            self._enable_wal(connection)
            current_version = connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0]
            if current_version not in (0, self.schema_version):
                raise UnsupportedSchemaVersionError(
                    "Unsupported quote-observation schema version: "
                    f"database is {current_version}, requested "
                    f"{self.schema_version}"
                )

            schema_sql = (
                _SCHEMA_V2_SQL
                if self.schema_version == HIERARCHY_SCHEMA_VERSION
                else _SCHEMA_SQL
            )
            connection.executescript(schema_sql)
            created_at_utc = _utc_text(_utc_now(), "created_at_utc")
            if self.schema_version == HIERARCHY_SCHEMA_VERSION:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO store_identity (
                        singleton,
                        schema_version,
                        session_date,
                        created_at_utc,
                        membership_contract
                    ) VALUES (1, ?, ?, ?, ?)
                    """,
                    (
                        self.schema_version,
                        self.session_date.isoformat(),
                        created_at_utc,
                        SAMPLING_HIERARCHY_CONTRACT,
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO store_identity (
                        singleton,
                        schema_version,
                        session_date,
                        created_at_utc
                    ) VALUES (1, ?, ?, ?)
                    """,
                    (
                        self.schema_version,
                        self.session_date.isoformat(),
                        created_at_utc,
                    ),
                )
            identity = connection.execute(
                "SELECT * FROM store_identity WHERE singleton = 1"
            ).fetchone()
            if identity["schema_version"] != self.schema_version:
                raise UnsupportedSchemaVersionError(
                    "store_identity schema version does not match"
                )
            if identity["session_date"] != self.session_date.isoformat():
                raise SessionMismatchError(
                    f"Database is bound to {identity['session_date']}, not "
                    f"{self.session_date.isoformat()}"
                )
            if (
                self.schema_version == HIERARCHY_SCHEMA_VERSION
                and identity["membership_contract"]
                != SAMPLING_HIERARCHY_CONTRACT
            ):
                raise UnsupportedSchemaVersionError(
                    "store_identity membership contract does not match"
                )
            connection.execute(
                f"PRAGMA user_version = {self.schema_version}"
            )

    def record_run(self, run: PollRunProvenance) -> RecordResult:
        """Record immutable software/configuration provenance for a process."""

        configuration_json = _json_text(dict(run.configuration))
        command_json = _json_text(list(run.command))
        values = (
            run.run_id,
            _utc_text(run.started_at, "started_at"),
            run.software_version,
            run.host,
            command_json,
            configuration_json,
        )
        fingerprint = _fingerprint(values)

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = self._duplicate_result(
                connection,
                table="poll_run",
                id_column="run_id",
                record_id=run.run_id,
                fingerprint=fingerprint,
            )
            if duplicate is not None:
                return duplicate
            connection.execute(
                """
                INSERT INTO poll_run (
                    run_id,
                    started_at_utc,
                    software_version,
                    host,
                    command_json,
                    configuration_json,
                    content_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (*values, fingerprint),
            )
        return RecordResult.INSERTED

    def record_channel_revision(
        self,
        revision: SamplingChannelRevision,
    ) -> RecordResult:
        """Record one immutable membership revision and all of its symbols."""

        self._require_schema_version(SCHEMA_VERSION)
        self._require_session(revision.session_date)
        metadata_json = _json_text(dict(revision.metadata))
        header = (
            revision.channel,
            revision.session_date.isoformat(),
            revision.revision,
            _utc_text(revision.effective_at, "effective_at"),
            len(revision.symbols),
            revision.source,
            revision.reason,
            metadata_json,
        )
        fingerprint = _fingerprint(
            {"header": header, "symbols": revision.symbols}
        )

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT content_sha256
                FROM sampling_channel_revision
                WHERE channel = ? AND session_date = ? AND revision = ?
                """,
                (
                    revision.channel,
                    revision.session_date.isoformat(),
                    revision.revision,
                ),
            ).fetchone()
            if existing is not None:
                if existing["content_sha256"] == fingerprint:
                    return RecordResult.ALREADY_PRESENT
                raise DuplicateRecordError(
                    "Channel revision already exists with different content: "
                    f"{revision.channel} r{revision.revision}"
                )

            connection.execute(
                """
                INSERT INTO sampling_channel_revision (
                    channel,
                    session_date,
                    revision,
                    effective_at_utc,
                    symbol_count,
                    source,
                    reason,
                    metadata_json,
                    content_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*header, fingerprint),
            )
            connection.executemany(
                """
                INSERT INTO sampling_channel_member (
                    channel,
                    session_date,
                    revision,
                    symbol,
                    ordinal
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (
                        revision.channel,
                        revision.session_date.isoformat(),
                        revision.revision,
                        symbol,
                        ordinal,
                    )
                    for ordinal, symbol in enumerate(revision.symbols)
                ),
            )
        return RecordResult.INSERTED

    def record_membership_revision(
        self,
        revision: SamplingHierarchyRevision,
    ) -> RecordResult:
        """Atomically publish one complete Uni/Focus/Hot hierarchy."""

        self._require_schema_version(HIERARCHY_SCHEMA_VERSION)
        if not isinstance(revision, SamplingHierarchyRevision):
            raise TypeError(
                "revision must be a SamplingHierarchyRevision"
            )
        self._require_session(revision.session_date)
        if revision.published_at is not None:
            raise ValueError(
                "published_at is assigned by the store and must be None"
            )

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_row = connection.execute(
                """
                SELECT *
                FROM sampling_membership_revision
                WHERE session_date = ? AND revision = ?
                """,
                (self.session_date.isoformat(), revision.revision),
            ).fetchone()
            if existing_row is not None:
                existing = self._membership_revision_from_row(
                    connection,
                    existing_row,
                )
                if existing.content_sha256 == revision.content_sha256:
                    return RecordResult.ALREADY_PRESENT
                raise DuplicateRecordError(
                    "Hierarchy revision already exists with different "
                    f"content: r{revision.revision}"
                )

            current_row = connection.execute(
                """
                SELECT *
                FROM sampling_membership_revision
                WHERE session_date = ?
                ORDER BY revision DESC
                LIMIT 1
                """,
                (self.session_date.isoformat(),),
            ).fetchone()
            current = (
                self._membership_revision_from_row(connection, current_row)
                if current_row is not None
                else None
            )
            committed = replace(revision, published_at=_utc_now())
            transition = assess_revision_candidate(current, committed)
            if transition is RevisionTransition.ALREADY_PRESENT:
                return RecordResult.ALREADY_PRESENT
            session_text = committed.session_date.isoformat()
            effective_text = _utc_text(
                committed.effective_at,
                "effective_at",
            )
            published_text = _utc_text(
                committed.published_at,
                "published_at",
            )
            metadata_json = _json_text(committed.metadata)
            connection.execute(
                """
                INSERT INTO sampling_membership_revision (
                    session_date,
                    revision,
                    effective_at_utc,
                    published_at_utc,
                    membership_contract,
                    source,
                    reason,
                    metadata_json,
                    content_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_text,
                    committed.revision,
                    effective_text,
                    published_text,
                    committed.contract,
                    committed.source,
                    committed.reason,
                    metadata_json,
                    committed.content_sha256,
                ),
            )

            for channel in SamplingChannel:
                symbols = committed.symbols_for(channel)
                channel_header = (
                    channel.value,
                    session_text,
                    committed.revision,
                    effective_text,
                    len(symbols),
                    committed.source,
                    committed.reason,
                    metadata_json,
                )
                channel_fingerprint = _fingerprint(
                    {"header": channel_header, "symbols": symbols}
                )
                connection.execute(
                    """
                    INSERT INTO sampling_channel_revision (
                        channel,
                        session_date,
                        revision,
                        effective_at_utc,
                        symbol_count,
                        source,
                        reason,
                        metadata_json,
                        content_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*channel_header, channel_fingerprint),
                )
                connection.executemany(
                    """
                    INSERT INTO sampling_channel_member (
                        channel,
                        session_date,
                        revision,
                        symbol,
                        ordinal
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            channel.value,
                            session_text,
                            committed.revision,
                            symbol,
                            ordinal,
                        )
                        for ordinal, symbol in enumerate(symbols)
                    ),
                )

        return RecordResult.INSERTED

    def record_skipped_slot(
        self,
        run_id: str,
        skipped: SkippedPollSlot,
    ) -> RecordResult:
        """Record one schema-v2 slot skipped for empty membership."""

        self._require_schema_version(HIERARCHY_SCHEMA_VERSION)
        _require_nonblank(run_id, "run_id")
        if not isinstance(skipped, SkippedPollSlot):
            raise TypeError("skipped must be a SkippedPollSlot")
        self._require_session(skipped.slot.session_date)
        if skipped.observed_at < skipped.slot.scheduled_at:
            raise ValueError("observed_at cannot precede scheduled_at")

        channel = skipped.slot.watchlist_kind.value
        session_text = skipped.slot.session_date.isoformat()
        header = (
            skipped.slot_id,
            run_id,
            channel,
            skipped.watchlist_revision,
            session_text,
            _utc_text(skipped.slot.scheduled_at, "scheduled_at"),
            _utc_text(
                skipped.membership_effective_at,
                "membership_effective_at",
            ),
            _utc_text(skipped.observed_at, "observed_at"),
            skipped.reason,
        )
        fingerprint = _fingerprint(header)

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM poll_run WHERE run_id = ?",
                (run_id,),
            ).fetchone() is None:
                raise UnknownRunError(f"Unknown polling run: {run_id}")

            revision_row = connection.execute(
                """
                SELECT effective_at_utc, symbol_count
                FROM sampling_channel_revision
                WHERE channel = ? AND session_date = ? AND revision = ?
                """,
                (
                    channel,
                    session_text,
                    skipped.watchlist_revision,
                ),
            ).fetchone()
            if revision_row is None:
                raise UnknownChannelRevisionError(
                    "Unknown sampling-channel revision: "
                    f"{channel} r{skipped.watchlist_revision}"
                )
            recorded_effective_at = _parse_utc(
                revision_row["effective_at_utc"]
            )
            if recorded_effective_at != skipped.membership_effective_at.astimezone(
                timezone.utc
            ):
                raise DuplicateRecordError(
                    "Skipped slot membership effective time differs from its "
                    "recorded channel revision"
                )
            latest_revision = self._latest_hierarchy_revision_number(
                connection,
                skipped.slot.scheduled_at,
            )
            if latest_revision != skipped.watchlist_revision:
                raise DuplicateRecordError(
                    "Skipped slot does not use the latest hierarchy revision "
                    f"effective at its scheduled slot: {channel} "
                    f"r{skipped.watchlist_revision}; expected r{latest_revision}"
                )
            if revision_row["symbol_count"] != 0:
                raise DuplicateRecordError(
                    "Polling slot cannot be skipped because its recorded "
                    f"membership is nonempty: {channel} "
                    f"r{skipped.watchlist_revision}"
                )

            if connection.execute(
                "SELECT 1 FROM quote_acquisition WHERE slot_id = ?",
                (skipped.slot_id,),
            ).fetchone() is not None:
                raise DuplicateRecordError(
                    "Polling slot already has an acquisition: "
                    f"{skipped.slot_id}"
                )

            duplicate = self._duplicate_result(
                connection,
                table="poll_slot_skip",
                id_column="slot_id",
                record_id=skipped.slot_id,
                fingerprint=fingerprint,
            )
            if duplicate is not None:
                return duplicate

            connection.execute(
                """
                INSERT INTO poll_slot_skip (
                    slot_id,
                    run_id,
                    channel,
                    channel_revision,
                    session_date,
                    scheduled_at_utc,
                    membership_effective_at_utc,
                    observed_at_utc,
                    reason,
                    content_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*header, fingerprint),
            )
        return RecordResult.INSERTED

    def record_acquisition(
        self,
        run_id: str,
        request: PollRequest,
        result: QuoteBatchResult,
        *,
        completed_at: datetime,
    ) -> RecordResult:
        """Atomically append one acquisition and every requested symbol."""

        _require_nonblank(run_id, "run_id")
        self._require_session(request.session_date)
        _require_aware(completed_at, "completed_at")
        if completed_at < request.dispatched_at:
            raise ValueError("completed_at cannot precede dispatched_at")

        result_symbols = tuple(item.symbol for item in result.results)
        if result_symbols != request.symbols:
            raise ValueError(
                "Quote results must match the PollRequest symbols in order"
            )

        rows = [
            self._observation_values(request.batch_id, ordinal, item)
            for ordinal, item in enumerate(result.results)
        ]
        channel = request.watchlist_kind.value
        header = (
            request.batch_id,
            run_id,
            channel,
            request.watchlist_revision,
            request.session_date.isoformat(),
            request.slot_id,
            _utc_text(request.slot.scheduled_at, "scheduled_at"),
            _utc_text(request.dispatched_at, "dispatched_at"),
            _utc_text(completed_at, "completed_at"),
            result.request_count,
            result.batch_size,
            len(request.symbols),
            _json_text(list(result.unexpected_symbols)),
        )
        fingerprint = _fingerprint({"header": header, "rows": rows})

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM poll_run WHERE run_id = ?",
                (run_id,),
            ).fetchone() is None:
                raise UnknownRunError(f"Unknown polling run: {run_id}")

            revision_row = connection.execute(
                """
                SELECT effective_at_utc
                FROM sampling_channel_revision
                WHERE channel = ? AND session_date = ? AND revision = ?
                """,
                (
                    channel,
                    request.session_date.isoformat(),
                    request.watchlist_revision,
                ),
            ).fetchone()
            if revision_row is None:
                raise UnknownChannelRevisionError(
                    "Unknown sampling-channel revision: "
                    f"{channel} r{request.watchlist_revision}"
                )
            binding_time = (
                request.slot.scheduled_at
                if self.schema_version == HIERARCHY_SCHEMA_VERSION
                else request.dispatched_at
            )
            if _parse_utc(revision_row["effective_at_utc"]) > (
                binding_time.astimezone(timezone.utc)
            ):
                boundary = (
                    "scheduled slot"
                    if self.schema_version == HIERARCHY_SCHEMA_VERSION
                    else "dispatch"
                )
                raise DuplicateRecordError(
                    "Polling acquisition uses a channel revision before "
                    f"its effective time at {boundary}"
                )
            if self.schema_version == HIERARCHY_SCHEMA_VERSION:
                latest_revision = self._latest_hierarchy_revision_number(
                    connection,
                    request.slot.scheduled_at,
                )
                if latest_revision != request.watchlist_revision:
                    raise DuplicateRecordError(
                        "Polling acquisition does not use the latest hierarchy "
                        "revision effective at its scheduled slot: "
                        f"{channel} r{request.watchlist_revision}; expected "
                        f"r{latest_revision}"
                    )

            member_rows = connection.execute(
                """
                SELECT symbol
                FROM sampling_channel_member
                WHERE channel = ? AND session_date = ? AND revision = ?
                ORDER BY ordinal
                """,
                (
                    channel,
                    request.session_date.isoformat(),
                    request.watchlist_revision,
                ),
            ).fetchall()
            recorded_symbols = tuple(row["symbol"] for row in member_rows)
            if recorded_symbols != request.symbols:
                raise DuplicateRecordError(
                    "PollRequest symbols differ from its recorded channel "
                    "revision"
                )

            slot_row = connection.execute(
                """
                SELECT acquisition_id
                FROM quote_acquisition
                WHERE slot_id = ?
                """,
                (request.slot_id,),
            ).fetchone()
            if (
                slot_row is not None
                and slot_row["acquisition_id"] != request.batch_id
            ):
                raise DuplicateRecordError(
                    "Polling slot already has another acquisition: "
                    f"{request.slot_id}"
                )
            if (
                self.schema_version == HIERARCHY_SCHEMA_VERSION
                and connection.execute(
                    "SELECT 1 FROM poll_slot_skip WHERE slot_id = ?",
                    (request.slot_id,),
                ).fetchone()
                is not None
            ):
                raise DuplicateRecordError(
                    "Polling slot is already recorded as skipped: "
                    f"{request.slot_id}"
                )

            duplicate = self._duplicate_result(
                connection,
                table="quote_acquisition",
                id_column="acquisition_id",
                record_id=request.batch_id,
                fingerprint=fingerprint,
            )
            if duplicate is not None:
                return duplicate

            connection.execute(
                """
                INSERT INTO quote_acquisition (
                    acquisition_id,
                    run_id,
                    channel,
                    channel_revision,
                    session_date,
                    slot_id,
                    scheduled_at_utc,
                    dispatched_at_utc,
                    completed_at_utc,
                    request_count,
                    batch_size,
                    requested_symbol_count,
                    unexpected_symbols_json,
                    content_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*header, fingerprint),
            )

            column_list = ", ".join(OBSERVATION_COLUMNS)
            placeholders = ", ".join(
                "?" for _ in range(9 + len(OBSERVATION_COLUMNS))
            )
            connection.executemany(
                f"""
                INSERT INTO quote_observation (
                    acquisition_id,
                    symbol,
                    ordinal,
                    status,
                    detail,
                    schwab_batch_number,
                    request_started_at_utc,
                    response_received_at_utc,
                    normalized_schema_version,
                    {column_list}
                ) VALUES ({placeholders})
                """,
                rows,
            )
        return RecordResult.INSERTED

    def get_acquisition(
        self,
        acquisition_id: str,
    ) -> StoredAcquisition | None:
        """Load one acquisition header by its stable polling batch ID."""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM quote_acquisition WHERE acquisition_id = ?",
                (acquisition_id,),
            ).fetchone()
        if row is None:
            return None
        return self._stored_acquisition(row)

    def acquisitions_in_replay_order(self) -> tuple[StoredAcquisition, ...]:
        """Load all completed acquisitions in deterministic event order."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM quote_acquisition
                ORDER BY scheduled_at_utc, channel, acquisition_id
                """
            ).fetchall()
        return tuple(self._stored_acquisition(row) for row in rows)

    def channel_revisions_in_effective_order(
        self,
    ) -> tuple[SamplingChannelRevision, ...]:
        """Load membership changes in deterministic replay order."""

        self._require_schema_version(SCHEMA_VERSION)
        revisions: list[SamplingChannelRevision] = []
        with self._connection() as connection:
            header_rows = connection.execute(
                """
                SELECT *
                FROM sampling_channel_revision
                ORDER BY effective_at_utc, channel, revision
                """
            ).fetchall()
            for row in header_rows:
                symbol_rows = connection.execute(
                    """
                    SELECT symbol
                    FROM sampling_channel_member
                    WHERE channel = ? AND session_date = ? AND revision = ?
                    ORDER BY ordinal
                    """,
                    (
                        row["channel"],
                        row["session_date"],
                        row["revision"],
                    ),
                ).fetchall()
                revisions.append(
                    SamplingChannelRevision(
                        channel=row["channel"],
                        session_date=date.fromisoformat(row["session_date"]),
                        revision=row["revision"],
                        effective_at=_parse_utc(row["effective_at_utc"]),
                        symbols=tuple(item["symbol"] for item in symbol_rows),
                        source=row["source"],
                        reason=row["reason"],
                        metadata=json.loads(row["metadata_json"]),
                    )
                )
        return tuple(revisions)

    def membership_revisions_in_effective_order(
        self,
    ) -> tuple[SamplingHierarchyRevision, ...]:
        """Load complete hierarchy revisions in deterministic event order."""

        self._require_schema_version(HIERARCHY_SCHEMA_VERSION)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM sampling_membership_revision
                ORDER BY effective_at_utc, revision
                """
            ).fetchall()
            return tuple(
                self._membership_revision_from_row(connection, row)
                for row in rows
            )

    def latest_membership_revision_effective_at(
        self,
        value: datetime,
    ) -> SamplingHierarchyRevision | None:
        """Return the newest hierarchy revision effective at ``value``.

        Pollers use the scheduled slot time for this lookup so a delayed
        dispatch cannot silently adopt a revision that became effective only
        after the slot.
        """

        self._require_schema_version(HIERARCHY_SCHEMA_VERSION)
        _require_aware(value, "value")
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM sampling_membership_revision
                WHERE session_date = ? AND effective_at_utc <= ?
                ORDER BY effective_at_utc DESC, revision DESC
                LIMIT 1
                """,
                (
                    self.session_date.isoformat(),
                    _utc_text(value, "value"),
                ),
            ).fetchone()
            if row is None:
                return None
            return self._membership_revision_from_row(connection, row)

    def _membership_revision_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> SamplingHierarchyRevision:
        channel_rows = connection.execute(
            """
            SELECT *
            FROM sampling_channel_revision
            WHERE session_date = ? AND revision = ?
            ORDER BY channel
            """,
            (row["session_date"], row["revision"]),
        ).fetchall()
        expected_channels = {channel.value for channel in SamplingChannel}
        actual_channels = {item["channel"] for item in channel_rows}
        if actual_channels != expected_channels or len(channel_rows) != 3:
            raise QuoteObservationStoreError(
                "Hierarchy revision does not contain exactly Uni, Focus, "
                f"and Hot: r{row['revision']}"
            )

        memberships: dict[str, tuple[str, ...]] = {}
        for channel_row in channel_rows:
            member_rows = connection.execute(
                """
                SELECT symbol
                FROM sampling_channel_member
                WHERE channel = ? AND session_date = ? AND revision = ?
                ORDER BY ordinal
                """,
                (
                    channel_row["channel"],
                    row["session_date"],
                    row["revision"],
                ),
            ).fetchall()
            symbols = tuple(item["symbol"] for item in member_rows)
            if len(symbols) != channel_row["symbol_count"]:
                raise QuoteObservationStoreError(
                    "Hierarchy channel member count does not match its "
                    f"header: {channel_row['channel']} r{row['revision']}"
                )
            expected_channel_header = (
                channel_row["channel"],
                row["session_date"],
                row["revision"],
                row["effective_at_utc"],
                len(symbols),
                row["source"],
                row["reason"],
                row["metadata_json"],
            )
            actual_channel_provenance = (
                channel_row["effective_at_utc"],
                channel_row["source"],
                channel_row["reason"],
                channel_row["metadata_json"],
            )
            expected_channel_provenance = (
                row["effective_at_utc"],
                row["source"],
                row["reason"],
                row["metadata_json"],
            )
            if actual_channel_provenance != expected_channel_provenance:
                raise QuoteObservationStoreError(
                    "Hierarchy channel header differs from its hierarchy "
                    f"header: {channel_row['channel']} r{row['revision']}"
                )
            expected_channel_fingerprint = _fingerprint(
                {
                    "header": expected_channel_header,
                    "symbols": symbols,
                }
            )
            if (
                channel_row["content_sha256"]
                != expected_channel_fingerprint
            ):
                raise QuoteObservationStoreError(
                    "Hierarchy channel content hash does not match: "
                    f"{channel_row['channel']} r{row['revision']}"
                )
            memberships[channel_row["channel"]] = symbols

        revision = SamplingHierarchyRevision(
            session_date=date.fromisoformat(row["session_date"]),
            revision=row["revision"],
            effective_at=_parse_utc(row["effective_at_utc"]),
            uni_symbols=memberships[SamplingChannel.UNI.value],
            focus_symbols=memberships[SamplingChannel.FOCUS.value],
            hot_symbols=memberships[SamplingChannel.HOT.value],
            source=row["source"],
            reason=row["reason"],
            metadata=json.loads(row["metadata_json"]),
            published_at=_parse_utc(row["published_at_utc"]),
            contract=row["membership_contract"],
        )
        if revision.content_sha256 != row["content_sha256"]:
            raise QuoteObservationStoreError(
                "Hierarchy revision content hash does not match: "
                f"r{row['revision']}"
            )
        return revision

    def _latest_hierarchy_revision_number(
        self,
        connection: sqlite3.Connection,
        value: datetime,
    ) -> int | None:
        """Return the newest global revision effective at ``value``."""

        row = connection.execute(
            """
            SELECT revision
            FROM sampling_membership_revision
            WHERE session_date = ? AND effective_at_utc <= ?
            ORDER BY effective_at_utc DESC, revision DESC
            LIMIT 1
            """,
            (
                self.session_date.isoformat(),
                _utc_text(value, "value"),
            ),
        ).fetchone()
        return None if row is None else row["revision"]

    @staticmethod
    def _stored_acquisition(row: sqlite3.Row) -> StoredAcquisition:
        return StoredAcquisition(
            acquisition_id=row["acquisition_id"],
            run_id=row["run_id"],
            channel=row["channel"],
            channel_revision=row["channel_revision"],
            session_date=date.fromisoformat(row["session_date"]),
            slot_id=row["slot_id"],
            scheduled_at_utc=_parse_utc(row["scheduled_at_utc"]),
            dispatched_at_utc=_parse_utc(row["dispatched_at_utc"]),
            completed_at_utc=_parse_utc(row["completed_at_utc"]),
            request_count=row["request_count"],
            batch_size=row["batch_size"],
            requested_symbol_count=row["requested_symbol_count"],
            unexpected_symbols=tuple(
                json.loads(row["unexpected_symbols_json"])
            ),
            content_sha256=row["content_sha256"],
        )

    def get_observations(
        self,
        acquisition_id: str,
    ) -> tuple[StoredQuoteObservation, ...]:
        """Load all symbol results for an acquisition in request order."""

        return self._query_observations(
            "WHERE observation.acquisition_id = ? ORDER BY observation.ordinal",
            (acquisition_id,),
        )

    def recent_observations(
        self,
        symbol: str,
        *,
        limit: int = 100,
    ) -> tuple[StoredQuoteObservation, ...]:
        """Load a symbol's latest observations across all channels."""

        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("symbol must not be blank")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        return self._query_observations(
            """
            WHERE observation.symbol = ?
            ORDER BY acquisition.scheduled_at_utc DESC
            LIMIT ?
            """,
            (normalized, limit),
        )

    def _query_observations(
        self,
        suffix: str,
        parameters: tuple[Any, ...],
    ) -> tuple[StoredQuoteObservation, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    observation.*,
                    acquisition.channel,
                    acquisition.channel_revision,
                    acquisition.scheduled_at_utc
                FROM quote_observation AS observation
                JOIN quote_acquisition AS acquisition
                    ON acquisition.acquisition_id = observation.acquisition_id
                {suffix}
                """,
                parameters,
            ).fetchall()
        return tuple(self._stored_observation(row) for row in rows)

    @staticmethod
    def _stored_observation(row: sqlite3.Row) -> StoredQuoteObservation:
        return StoredQuoteObservation(
            acquisition_id=row["acquisition_id"],
            channel=row["channel"],
            channel_revision=row["channel_revision"],
            scheduled_at_utc=_parse_utc(row["scheduled_at_utc"]),
            symbol=row["symbol"],
            ordinal=row["ordinal"],
            status=row["status"],
            detail=row["detail"],
            schwab_batch_number=row["schwab_batch_number"],
            request_started_at_utc=_parse_utc(
                row["request_started_at_utc"]
            ),
            response_received_at_utc=(
                _parse_utc(row["response_received_at_utc"])
                if row["response_received_at_utc"] is not None
                else None
            ),
            values={column: row[column] for column in OBSERVATION_COLUMNS},
        )

    @staticmethod
    def _observation_values(
        acquisition_id: str,
        ordinal: int,
        result: QuoteResult,
    ) -> tuple[Any, ...]:
        return (
            acquisition_id,
            result.symbol,
            ordinal,
            result.status.value,
            result.detail,
            result.batch_number,
            _utc_text(
                result.request_started_at_utc,
                "request_started_at_utc",
            ),
            (
                _utc_text(
                    result.response_received_at_utc,
                    "response_received_at_utc",
                )
                if result.response_received_at_utc is not None
                else None
            ),
            SCHEMA_VERSION,
            *_normalized_quote_values(result),
        )

    def _require_session(self, record_date: date) -> None:
        if record_date != self.session_date:
            raise SessionMismatchError(
                f"Record is for {record_date}, but database is bound to "
                f"{self.session_date}"
            )

    def _require_schema_version(self, expected: int) -> None:
        if self.schema_version != expected:
            raise UnsupportedSchemaVersionError(
                f"Operation requires schema version {expected}; store is "
                f"configured for version {self.schema_version}"
            )

    @staticmethod
    def _duplicate_result(
        connection: sqlite3.Connection,
        *,
        table: str,
        id_column: str,
        record_id: str,
        fingerprint: str,
    ) -> RecordResult | None:
        row = connection.execute(
            f"SELECT content_sha256 FROM {table} WHERE {id_column} = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            return None
        if row["content_sha256"] == fingerprint:
            return RecordResult.ALREADY_PRESENT
        raise DuplicateRecordError(
            f"{table} ID already exists with different content: {record_id}"
        )


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS store_identity (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL,
    session_date TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS poll_run (
    run_id TEXT PRIMARY KEY,
    started_at_utc TEXT NOT NULL,
    software_version TEXT NOT NULL,
    host TEXT,
    command_json TEXT NOT NULL,
    configuration_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sampling_channel_revision (
    channel TEXT NOT NULL,
    session_date TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    effective_at_utc TEXT NOT NULL,
    symbol_count INTEGER NOT NULL CHECK (symbol_count > 0),
    source TEXT NOT NULL,
    reason TEXT,
    metadata_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    PRIMARY KEY (channel, session_date, revision)
);

CREATE TABLE IF NOT EXISTS sampling_channel_member (
    channel TEXT NOT NULL,
    session_date TEXT NOT NULL,
    revision INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    PRIMARY KEY (channel, session_date, revision, symbol),
    UNIQUE (channel, session_date, revision, ordinal),
    FOREIGN KEY (channel, session_date, revision)
        REFERENCES sampling_channel_revision (channel, session_date, revision)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS quote_acquisition (
    acquisition_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES poll_run (run_id),
    channel TEXT NOT NULL,
    channel_revision INTEGER NOT NULL,
    session_date TEXT NOT NULL,
    slot_id TEXT NOT NULL,
    scheduled_at_utc TEXT NOT NULL,
    dispatched_at_utc TEXT NOT NULL,
    completed_at_utc TEXT NOT NULL,
    request_count INTEGER NOT NULL CHECK (request_count >= 0),
    batch_size INTEGER NOT NULL CHECK (batch_size > 0),
    requested_symbol_count INTEGER NOT NULL
        CHECK (requested_symbol_count > 0),
    unexpected_symbols_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    UNIQUE (slot_id),
    FOREIGN KEY (channel, session_date, channel_revision)
        REFERENCES sampling_channel_revision (channel, session_date, revision)
);

CREATE INDEX IF NOT EXISTS quote_acquisition_session_channel_idx
ON quote_acquisition (session_date, channel, scheduled_at_utc);

CREATE TABLE IF NOT EXISTS quote_observation (
    acquisition_id TEXT NOT NULL
        REFERENCES quote_acquisition (acquisition_id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    status TEXT NOT NULL,
    detail TEXT,
    schwab_batch_number INTEGER NOT NULL,
    request_started_at_utc TEXT NOT NULL,
    response_received_at_utc TEXT,
    normalized_schema_version INTEGER NOT NULL,
    asset_main_type TEXT,
    asset_sub_type TEXT,
    realtime INTEGER,
    extended_bid_price REAL,
    extended_ask_price REAL,
    extended_mark REAL,
    extended_last_price REAL,
    extended_last_size INTEGER,
    extended_total_volume INTEGER,
    extended_quote_time_ms INTEGER,
    extended_trade_time_ms INTEGER,
    quote_bid_price REAL,
    quote_ask_price REAL,
    quote_mark REAL,
    quote_last_price REAL,
    quote_bid_size INTEGER,
    quote_ask_size INTEGER,
    quote_last_size INTEGER,
    quote_total_volume INTEGER,
    quote_security_status TEXT,
    quote_open_price REAL,
    quote_high_price REAL,
    quote_low_price REAL,
    quote_close_price REAL,
    quote_net_change REAL,
    quote_net_percent_change REAL,
    quote_time_ms INTEGER,
    quote_trade_time_ms INTEGER,
    quote_bid_time_ms INTEGER,
    quote_ask_time_ms INTEGER,
    post_market_change REAL,
    post_market_percent_change REAL,
    regular_market_last_price REAL,
    regular_market_last_size INTEGER,
    regular_market_trade_time_ms INTEGER,
    regular_market_net_change REAL,
    regular_market_percent_change REAL,
    shares_outstanding INTEGER,
    avg_10_days_volume REAL,
    avg_1_year_volume REAL,
    exchange TEXT,
    exchange_name TEXT,
    description TEXT,
    PRIMARY KEY (acquisition_id, symbol),
    UNIQUE (acquisition_id, ordinal)
);

CREATE INDEX IF NOT EXISTS quote_observation_symbol_idx
ON quote_observation (symbol, acquisition_id);
"""


_SCHEMA_V2_SQL = f"""
CREATE TABLE IF NOT EXISTS store_identity (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 2),
    session_date TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    membership_contract TEXT NOT NULL
        CHECK (membership_contract = '{SAMPLING_HIERARCHY_CONTRACT}')
);

CREATE TABLE IF NOT EXISTS poll_run (
    run_id TEXT PRIMARY KEY,
    started_at_utc TEXT NOT NULL,
    software_version TEXT NOT NULL,
    host TEXT,
    command_json TEXT NOT NULL,
    configuration_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sampling_membership_revision (
    session_date TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    effective_at_utc TEXT NOT NULL,
    published_at_utc TEXT NOT NULL,
    membership_contract TEXT NOT NULL
        CHECK (membership_contract = '{SAMPLING_HIERARCHY_CONTRACT}'),
    source TEXT NOT NULL,
    reason TEXT,
    metadata_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    PRIMARY KEY (session_date, revision)
);

CREATE TABLE IF NOT EXISTS sampling_channel_revision (
    channel TEXT NOT NULL CHECK (channel IN ('uni', 'focus', 'hot')),
    session_date TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    effective_at_utc TEXT NOT NULL,
    symbol_count INTEGER NOT NULL CHECK (symbol_count >= 0),
    source TEXT NOT NULL,
    reason TEXT,
    metadata_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    PRIMARY KEY (channel, session_date, revision),
    FOREIGN KEY (session_date, revision)
        REFERENCES sampling_membership_revision (session_date, revision)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sampling_channel_member (
    channel TEXT NOT NULL,
    session_date TEXT NOT NULL,
    revision INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    PRIMARY KEY (channel, session_date, revision, symbol),
    UNIQUE (channel, session_date, revision, ordinal),
    FOREIGN KEY (channel, session_date, revision)
        REFERENCES sampling_channel_revision (channel, session_date, revision)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS poll_slot_skip (
    slot_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES poll_run (run_id),
    channel TEXT NOT NULL,
    channel_revision INTEGER NOT NULL,
    session_date TEXT NOT NULL,
    scheduled_at_utc TEXT NOT NULL,
    membership_effective_at_utc TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    reason TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    FOREIGN KEY (channel, session_date, channel_revision)
        REFERENCES sampling_channel_revision (channel, session_date, revision)
);

CREATE TABLE IF NOT EXISTS quote_acquisition (
    acquisition_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES poll_run (run_id),
    channel TEXT NOT NULL,
    channel_revision INTEGER NOT NULL,
    session_date TEXT NOT NULL,
    slot_id TEXT NOT NULL,
    scheduled_at_utc TEXT NOT NULL,
    dispatched_at_utc TEXT NOT NULL,
    completed_at_utc TEXT NOT NULL,
    request_count INTEGER NOT NULL CHECK (request_count >= 0),
    batch_size INTEGER NOT NULL CHECK (batch_size > 0),
    requested_symbol_count INTEGER NOT NULL
        CHECK (requested_symbol_count > 0),
    unexpected_symbols_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    UNIQUE (slot_id),
    FOREIGN KEY (channel, session_date, channel_revision)
        REFERENCES sampling_channel_revision (channel, session_date, revision)
);

CREATE INDEX IF NOT EXISTS quote_acquisition_session_channel_idx
ON quote_acquisition (session_date, channel, scheduled_at_utc);

CREATE TABLE IF NOT EXISTS quote_observation (
    acquisition_id TEXT NOT NULL
        REFERENCES quote_acquisition (acquisition_id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    status TEXT NOT NULL,
    detail TEXT,
    schwab_batch_number INTEGER NOT NULL,
    request_started_at_utc TEXT NOT NULL,
    response_received_at_utc TEXT,
    normalized_schema_version INTEGER NOT NULL,
    asset_main_type TEXT,
    asset_sub_type TEXT,
    realtime INTEGER,
    extended_bid_price REAL,
    extended_ask_price REAL,
    extended_mark REAL,
    extended_last_price REAL,
    extended_last_size INTEGER,
    extended_total_volume INTEGER,
    extended_quote_time_ms INTEGER,
    extended_trade_time_ms INTEGER,
    quote_bid_price REAL,
    quote_ask_price REAL,
    quote_mark REAL,
    quote_last_price REAL,
    quote_bid_size INTEGER,
    quote_ask_size INTEGER,
    quote_last_size INTEGER,
    quote_total_volume INTEGER,
    quote_security_status TEXT,
    quote_open_price REAL,
    quote_high_price REAL,
    quote_low_price REAL,
    quote_close_price REAL,
    quote_net_change REAL,
    quote_net_percent_change REAL,
    quote_time_ms INTEGER,
    quote_trade_time_ms INTEGER,
    quote_bid_time_ms INTEGER,
    quote_ask_time_ms INTEGER,
    post_market_change REAL,
    post_market_percent_change REAL,
    regular_market_last_price REAL,
    regular_market_last_size INTEGER,
    regular_market_trade_time_ms INTEGER,
    regular_market_net_change REAL,
    regular_market_percent_change REAL,
    shares_outstanding INTEGER,
    avg_10_days_volume REAL,
    avg_1_year_volume REAL,
    exchange TEXT,
    exchange_name TEXT,
    description TEXT,
    PRIMARY KEY (acquisition_id, symbol),
    UNIQUE (acquisition_id, ordinal)
);

CREATE INDEX IF NOT EXISTS quote_observation_symbol_idx
ON quote_observation (symbol, acquisition_id);
"""
