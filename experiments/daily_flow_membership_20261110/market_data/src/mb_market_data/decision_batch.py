"""
Batch assembly of immutable decision-time snapshots.

This module joins:

    TosWatchlist
        +
    QuoteBatchResult
        |
        v
    DecisionSnapshotBatch

No network requests, authentication, scheduling, ranking, or database
writes occur here.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone

from mb_market_data.decision_snapshot import (
    DecisionSnapshot,
    build_decision_snapshot,
)
from mb_market_data.schwab_quotes import (
    QuoteBatchResult,
    QuoteStatus,
)
from mb_market_data.tos_watchlist import (
    OVDecisionStatus,
    TosWatchlist,
)


UTC = timezone.utc


class DecisionBatchError(RuntimeError):
    """Decision-snapshot batch assembly failed."""


@dataclass(frozen=True)
class DecisionSnapshotBatch:
    """Immutable collection of per-symbol decision snapshots."""

    trade_date: date
    tos_observed_at_utc: datetime
    snapshots: tuple[DecisionSnapshot, ...]

    quote_request_count: int
    quote_batch_size: int

    unexpected_quote_symbols: tuple[str, ...]
    quote_results_not_in_watchlist: tuple[str, ...]

    def by_symbol(self) -> dict[str, DecisionSnapshot]:
        """Return snapshots keyed by symbol."""

        return {
            snapshot.symbol: snapshot
            for snapshot in self.snapshots
        }

    def ov_status_counts(
        self,
    ) -> Counter[OVDecisionStatus]:
        """Count ToS OV_DECISION statuses."""

        return Counter(
            snapshot.ov_decision_status
            for snapshot in self.snapshots
        )

    def quote_status_counts(
        self,
    ) -> Counter[QuoteStatus]:
        """Count Schwab quote acquisition statuses."""

        return Counter(
            snapshot.quote_status
            for snapshot in self.snapshots
        )

    @property
    def source_ready_snapshots(
        self,
    ) -> tuple[DecisionSnapshot, ...]:
        """
        Snapshots having both currently required source inputs.

        This does not mean a symbol passes later strategy filters.
        """

        return tuple(
            snapshot
            for snapshot in self.snapshots
            if (
                snapshot.has_usable_ov_decision
                and snapshot.has_schwab_quote
            )
        )

    @property
    def source_unready_snapshots(
        self,
    ) -> tuple[DecisionSnapshot, ...]:
        """Snapshots lacking OV_DECISION or a Schwab quote."""

        return tuple(
            snapshot
            for snapshot in self.snapshots
            if not (
                snapshot.has_usable_ov_decision
                and snapshot.has_schwab_quote
            )
        )


def _normalize_utc(
    value: datetime,
) -> datetime:
    """Require an aware datetime and normalize it to UTC."""

    if not isinstance(value, datetime):
        raise TypeError(
            "tos_observed_at_utc must be a datetime"
        )

    if (
        value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(
            "tos_observed_at_utc must be timezone-aware"
        )

    return value.astimezone(UTC)


def build_decision_snapshot_batch(
    *,
    trade_date: date,
    watchlist: TosWatchlist,
    quote_batch: QuoteBatchResult,
    tos_observed_at_utc: datetime,
) -> DecisionSnapshotBatch:
    """
    Assemble per-symbol snapshots from ToS and Schwab results.

    Every ToS symbol must have exactly one QuoteResult. This is a useful
    invariant because fetch_quotes_batched() promises an explicit result
    for every requested symbol.

    Extra quote results are permitted but are reported explicitly.
    """

    observed_utc = _normalize_utc(
        tos_observed_at_utc
    )

    watchlist_symbols = [
        row.symbol.strip().upper()
        for row in watchlist.rows
    ]

    if len(set(watchlist_symbols)) != len(
        watchlist_symbols
    ):
        raise DecisionBatchError(
            "ThinkOrSwim Watchlist contains "
            "duplicate symbols."
        )

    quote_symbols = [
        result.symbol.strip().upper()
        for result in quote_batch.results
    ]

    if len(set(quote_symbols)) != len(
        quote_symbols
    ):
        raise DecisionBatchError(
            "Quote batch contains duplicate "
            "symbol results."
        )

    quote_by_symbol = quote_batch.by_symbol()

    missing_quote_results = [
        symbol
        for symbol in watchlist_symbols
        if symbol not in quote_by_symbol
    ]

    if missing_quote_results:
        raise DecisionBatchError(
            "Quote batch is missing explicit result(s) "
            "for ToS symbol(s): "
            + " ".join(missing_quote_results)
        )

    watchlist_symbol_set = set(
        watchlist_symbols
    )

    quote_results_not_in_watchlist = tuple(
        sorted(
            symbol
            for symbol in quote_by_symbol
            if symbol not in watchlist_symbol_set
        )
    )

    snapshots: list[DecisionSnapshot] = []

    for row in watchlist.rows:
        symbol = row.symbol.strip().upper()

        snapshot = build_decision_snapshot(
            trade_date=trade_date,
            tos_row=row,
            quote_result=quote_by_symbol[symbol],
            tos_observed_at_utc=observed_utc,
        )

        snapshots.append(snapshot)

    return DecisionSnapshotBatch(
        trade_date=trade_date,
        tos_observed_at_utc=observed_utc,
        snapshots=tuple(snapshots),
        quote_request_count=(
            quote_batch.request_count
        ),
        quote_batch_size=(
            quote_batch.batch_size
        ),
        unexpected_quote_symbols=(
            quote_batch.unexpected_symbols
        ),
        quote_results_not_in_watchlist=(
            quote_results_not_in_watchlist
        ),
    )
