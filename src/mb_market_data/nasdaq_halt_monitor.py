from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

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
    Track volatility-pause symbols already observed during one ET date.

    The monitor does not fetch data itself and does not perform any
    Watchlist action. It only determines which qualifying symbols are new.
    """

    session_date: date | None = None
    seen_symbols: set[str] = field(default_factory=set)

    def reset(self, session_date: date) -> None:
        self.session_date = session_date
        self.seen_symbols.clear()

    def new_symbols(
        self,
        records: tuple[HaltRecord, ...] | list[HaltRecord],
        *,
        session_date: date,
    ) -> tuple[str, ...]:
        """
        Return qualifying volatility-pause symbols not previously seen
        during session_date.

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

        new_symbols = tuple(
            symbol
            for symbol in symbols
            if symbol not in self.seen_symbols
        )

        self.seen_symbols.update(new_symbols)

        return new_symbols
