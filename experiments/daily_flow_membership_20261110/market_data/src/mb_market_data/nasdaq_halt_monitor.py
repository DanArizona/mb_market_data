from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from collections.abc import Iterable

from mb_market_data.nasdaq_halts import (
    HaltRecord,
    VOLATILITY_REASON_CODES,
    records_for_date,
    records_with_reason_codes,
    unique_halt_events,
    unique_symbols,
)


@dataclass
class NasdaqHaltMonitor:
    """
    Track volatility-pause symbols acknowledged during one ET date.

    Detection and acknowledgement are deliberately separate.

    A symbol is not considered seen until mark_seen() is called.
    This allows a caller to retry a failed downstream Watchlist
    submission on the next poll.
    """

    session_date: date | None = None
    seen_symbols: set[str] = field(default_factory=set)

    def reset(
        self,
        session_date: date,
    ) -> None:
        """
        Start a new monitoring session for session_date.
        """
        self.session_date = session_date
        self.seen_symbols.clear()

    def pending_symbols(
        self,
        records: tuple[HaltRecord, ...] | list[HaltRecord],
        *,
        session_date: date,
    ) -> tuple[str, ...]:
        """
        Return qualifying volatility-pause symbols that have
        not yet been acknowledged.

        This method does NOT modify seen_symbols.

        If session_date changes, state is automatically reset.
        """
        if self.session_date != session_date:
            self.reset(session_date)

        session_records = records_for_date(
            records,
            session_date,
        )

        volatility_records = records_with_reason_codes(
            session_records,
            VOLATILITY_REASON_CODES,
        )

        unique_events = unique_halt_events(
            volatility_records,
        )

        symbols = unique_symbols(
            unique_events,
        )

        return tuple(
            symbol
            for symbol in symbols
            if symbol not in self.seen_symbols
        )

    def mark_seen(
        self,
        symbols: Iterable[str],
        *,
        session_date: date,
    ) -> None:
        """
        Acknowledge symbols as successfully handled.

        If session_date changes, state is reset before the
        supplied symbols are recorded.
        """
        if self.session_date != session_date:
            self.reset(session_date)

        for symbol in symbols:
            normalized = symbol.strip()

            if normalized:
                self.seen_symbols.add(normalized)

    def new_symbols(
        self,
        records: tuple[HaltRecord, ...] | list[HaltRecord],
        *,
        session_date: date,
    ) -> tuple[str, ...]:
        """
        Compatibility helper.

        Detect pending symbols and immediately acknowledge them.

        New code that depends on successful downstream processing
        should instead use:

            pending_symbols(...)
            mark_seen(...)
        """
        symbols = self.pending_symbols(
            records,
            session_date=session_date,
        )

        self.mark_seen(
            symbols,
            session_date=session_date,
        )

        return symbols
