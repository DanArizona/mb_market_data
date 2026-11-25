"""Bounded, controller-owned scheduling contract for selected-symbol OHLCV.

This module creates no Schwab client and contains no OAuth, password prompt,
background thread, service launcher, or production-run authorization. An
explicit owner supplies its authenticated client, selected symbol and clock.
A tick is called by that owner; never by the read-only Overlay viewer.

Every attempt is reserved in a nonsecret, persistent ledger BEFORE the API
call, preventing duplicate requests if an owned worker restarts. The owner
must independently enforce the lifetime/heartbeat and API-rate budget.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Callable

from .acquisition_health import atomic_json
from .observation_overlay import ET
from .overlay_live_ohlcv import (
    LiveOHLCVNotReady, acquire_live_ohlcv, publish_live_ohlcv,
    _symbol,
)

UTC = timezone.utc
SLOT = timedelta(minutes=5)
DEFAULT_GRACE = timedelta(seconds=5)
FINAL_SLOT_WINDOW = timedelta(minutes=2)
MAX_SYMBOLS_PER_SLOT = 2
LEDGER_CONTRACT = "overlay-owned-ohlcv-schedule-v1"


@dataclass(frozen=True)
class OHLCVTickerResult:
    status: str
    symbol: str | None
    slot_close_et: datetime | None
    bundle_path: Path | None = None
    candle_count: int = 0
    error_type: str | None = None


def _aware_et(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{label} must be an offset-aware datetime")
    return value.astimezone(ET)


def completed_slot(
    now: datetime, *, opens_at: datetime, closes_at: datetime,
    grace: timedelta = DEFAULT_GRACE,
) -> datetime | None:
    """Most recent closed five-minute slot at least grace seconds in the past.

    Opening and closing times come from the authoritative exchange session
    plan, not a fixed 16:00 assumption (early-close sessions are supported).
    """
    at = _aware_et(now, "now")
    start = _aware_et(opens_at, "opens_at")
    end = _aware_et(closes_at, "closes_at")
    if start.date() != end.date() or end <= start:
        raise ValueError("OHLCV schedule must be a single ET session")
    if start.second or start.microsecond or start.minute % 5:
        raise ValueError("OHLCV opening must be on a five-minute boundary")
    if end.second or end.microsecond or end.minute % 5:
        raise ValueError("OHLCV close must be on a five-minute boundary")
    if not isinstance(grace, timedelta) or not timedelta(0) <= grace <= timedelta(minutes=1):
        raise ValueError("OHLCV grace must be between zero and sixty seconds")
    # Permit a bounded late capture of the final completed interval. The
    # price-history cutoff is still the exchange close, never after it.
    if at.date() != start.date() or at < start + SLOT + grace or at > end + FINAL_SLOT_WINDOW:
        return None
    elapsed = (min(at - grace, end) - start) // SLOT
    if elapsed < 1:
        return None
    slot = start + elapsed * SLOT
    return min(slot, end)


class OwnedOHLCVSchedule:
    """One-owner one-symbol-per-slot acquisition gate.

    Each instance is bound to an explicit ET session and independent output.
    It does not start/stop polling; only the daily controller can supply the
    client and call tick after the authorized operator selection is known.
    """

    def __init__(
        self, *, session_date: date, opens_at: datetime, closes_at: datetime,
        state_root: str | Path, evidence_root: str | Path,
        max_symbols_per_slot: int = MAX_SYMBOLS_PER_SLOT,
        grace: timedelta = DEFAULT_GRACE,
    ) -> None:
        self.session_date = session_date
        self.opens_at = _aware_et(opens_at, "opens_at")
        self.closes_at = _aware_et(closes_at, "closes_at")
        self.state_root = Path(state_root).resolve()
        self.evidence_root = Path(evidence_root).resolve()
        if self.opens_at.date() != session_date or self.closes_at.date() != session_date:
            raise ValueError("OHLCV plan date differs from session date")
        if self.evidence_root == self.state_root:
            raise ValueError("OHLCV evidence and scheduling state must be separate")
        if type(max_symbols_per_slot) is not int or not 1 <= max_symbols_per_slot <= 2:
            raise ValueError("OHLCV budget must be one or two symbols per five minutes")
        self.max_symbols_per_slot = max_symbols_per_slot
        self.grace = grace
        self.ledger_path = self.state_root / "ledger.json"
        # Ensure malformed schedule is rejected BEFORE creating any output.
        completed_slot(
            self.opens_at + SLOT + grace,
            opens_at=self.opens_at, closes_at=self.closes_at, grace=grace,
        )

    def _read(self) -> dict:
        if not self.ledger_path.exists():
            return {"contract": LEDGER_CONTRACT,
                    "session_date": self.session_date.isoformat(),
                    "attempts": []}
        ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        if (
            not isinstance(ledger, dict)
            or ledger.get("contract") != LEDGER_CONTRACT
            or ledger.get("session_date") != self.session_date.isoformat()
            or not isinstance(ledger.get("attempts"), list)
        ):
            raise ValueError("OHLCV ledger identity or structure invalid")
        return ledger

    def _write(self, ledger: dict) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        atomic_json(self.ledger_path, ledger)

    def tick(
        self, *, client: Any,
        selected_symbol: str | None,
        now: datetime,
        now_factory: Callable[[], datetime] | None = None,
        owner_active: Callable[[], bool] = lambda: True,
    ) -> OHLCVTickerResult:
        """At most one attempt per symbol per completed interval.

        An explicitly injected live owner must guard this function with its
        exclusive service lock. An unsuccessful provider request waits for
        the next slot rather than causing a storm of retries.
        """
        slot = completed_slot(
            now, opens_at=self.opens_at,
            closes_at=self.closes_at, grace=self.grace,
        )
        if slot is None:
            return OHLCVTickerResult("NOT_DUE", None, None)
        if not owner_active():
            return OHLCVTickerResult("OWNER_UNAVAILABLE", None, slot)
        if selected_symbol is None or not str(selected_symbol).strip():
            return OHLCVTickerResult("NO_SELECTION", None, slot)
        symbol = _symbol(selected_symbol)
        ledger = self._read()
        key = slot.isoformat()
        attempts = [
            row for row in ledger["attempts"]
            if isinstance(row, dict) and row.get("slot_close_et") == key
        ]
        if any(row.get("symbol") == symbol for row in attempts):
            return OHLCVTickerResult("ALREADY_ATTEMPTED", symbol, slot)
        if len(attempts) >= self.max_symbols_per_slot:
            return OHLCVTickerResult("BUDGET_EXHAUSTED", symbol, slot)
        # Durable reservation comes before any SDK request, for crash-safe
        # at-most-once semantics across an explicit worker restart.
        attempt = {
            "slot_close_et": key,
            "symbol": symbol,
            "state": "RESERVED",
            "reserved_at_utc": _aware_et(now, "now").astimezone(UTC).isoformat(),
        }
        ledger["attempts"].append(attempt)
        self._write(ledger)
        try:
            acquisition = acquire_live_ohlcv(
                client, symbol=symbol, session_date=self.session_date,
                session_end_et=self.closes_at,
                **({"now_factory": now_factory} if now_factory is not None else {}),
            )
            path = publish_live_ohlcv(self.evidence_root, acquisition)
            attempt["state"] = "PUBLISHED"
            attempt["bundle_name"] = path.name
            attempt["candle_count"] = len(acquisition.cache.candles)
            self._write(ledger)
            return OHLCVTickerResult(
                "PUBLISHED", symbol, slot, path,
                len(acquisition.cache.candles),
            )
        except Exception as error:
            # Do not persist SDK exception messages, HTTP bodies or OAuth data.
            attempt["state"] = (
                "NOT_READY" if isinstance(error, LiveOHLCVNotReady)
                else "FAILED"
            )
            attempt["error_type"] = type(error).__name__
            self._write(ledger)
            return OHLCVTickerResult(
                attempt["state"], symbol, slot,
                error_type=type(error).__name__,
            )
