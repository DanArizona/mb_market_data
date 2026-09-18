"""Deterministic finalization of a next-session daily stock universe.

The live probes freeze two inputs:

* the complete Nasdaq Trader symbol directory; and
* one enriched Schwab close snapshot for each non-ETF, non-test candidate.

This module joins those frozen inputs and records one explainable decision for
every source symbol.  It performs no network access, so the same inputs and
configuration always produce the same decisions.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping


SELECTOR_VERSION = "daily-universe-v1"


@dataclass(frozen=True)
class UniverseFilterConfig:
    """Inclusive numeric limits used by the provisional selector."""

    minimum_volume: Decimal = Decimal("10000")
    minimum_close: Decimal = Decimal("0.10")
    minimum_market_cap: Decimal = Decimal("4000000")
    maximum_market_cap: Decimal = Decimal("40000000")

    def __post_init__(self) -> None:
        if self.minimum_volume < 0:
            raise ValueError("minimum_volume must not be negative")
        if self.minimum_close < 0:
            raise ValueError("minimum_close must not be negative")
        if self.minimum_market_cap < 0:
            raise ValueError("minimum_market_cap must not be negative")
        if self.maximum_market_cap < self.minimum_market_cap:
            raise ValueError(
                "maximum_market_cap must be greater than or equal to "
                "minimum_market_cap"
            )


@dataclass(frozen=True)
class UniverseDecision:
    symbol: str
    decision: str
    primary_reason: str
    reason_codes: str
    security_name: str
    nasdaq_source: str
    listing_exchange_code: str
    listing_exchange_name: str
    market_category: str
    financial_status: str
    etf: str
    test_issue: str
    acquisition_status: str
    acquisition_detail: str
    close_price: str
    total_volume: str
    instrument_status: str
    direct_market_cap: str

    @property
    def included(self) -> bool:
        return self.decision == "include"


DECISION_LEDGER_FIELDS = list(UniverseDecision.__dataclass_fields__)


def normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def parse_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None

    text = str(value).replace("$", "").replace(",", "").strip()
    if not text:
        return None

    try:
        number = Decimal(text)
    except InvalidOperation:
        return None

    if not number.is_finite():
        return None

    return number


def decimal_text(value: Decimal | None) -> str:
    if value is None:
        return ""

    return format(value, "f")


def is_yes(value: Any) -> bool:
    return str(value or "").strip().upper() == "Y"


def read_csv_rows(path: str | Path) -> list[dict[str, str]]:
    csv_path = Path(path)

    with csv_path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {csv_path}")
        if "symbol" not in reader.fieldnames:
            raise ValueError(f"CSV has no 'symbol' column: {csv_path}")
        return list(reader)


def index_unique_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    input_name: str,
) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}

    for row_number, row in enumerate(rows, start=2):
        symbol = normalize_symbol(row.get("symbol"))
        if not symbol:
            raise ValueError(
                f"{input_name} contains a blank symbol at CSV row {row_number}"
            )
        if symbol in indexed:
            raise ValueError(
                f"{input_name} contains duplicate symbol {symbol!r}"
            )
        indexed[symbol] = row

    return indexed


def _source_value(
    source: Mapping[str, Any],
    enriched: Mapping[str, Any] | None,
    *names: str,
) -> str:
    for row in (source, enriched or {}):
        for name in names:
            value = row.get(name)
            if value is not None and str(value).strip():
                return str(value).strip()
    return ""


def decide_daily_universe(
    source_rows: Iterable[Mapping[str, Any]],
    enriched_rows: Iterable[Mapping[str, Any]],
    config: UniverseFilterConfig | None = None,
) -> tuple[UniverseDecision, ...]:
    """Return one symbol-sorted decision per Nasdaq source symbol."""

    config = config or UniverseFilterConfig()
    sources = index_unique_rows(source_rows, input_name="symbol directory")
    enriched = index_unique_rows(enriched_rows, input_name="market-data snapshot")

    unknown = sorted(set(enriched) - set(sources))
    if unknown:
        preview = ", ".join(unknown[:10])
        suffix = "..." if len(unknown) > 10 else ""
        raise ValueError(
            "market-data snapshot contains symbols absent from the symbol "
            f"directory: {preview}{suffix}"
        )

    decisions: list[UniverseDecision] = []

    for symbol in sorted(sources):
        source = sources[symbol]
        market = enriched.get(symbol)
        reasons: list[str] = []

        if is_yes(source.get("test_issue")):
            reasons.append("test_issue")
        if is_yes(source.get("etf")):
            reasons.append("etf")

        acquisition_status = _source_value(
            source, market, "acquisition_status"
        )
        acquisition_detail = _source_value(
            source, market, "acquisition_detail"
        )
        instrument_status = _source_value(
            source, market, "instrument_status"
        )

        close = parse_decimal(market.get("close_price")) if market else None
        volume = parse_decimal(market.get("total_volume")) if market else None
        market_cap = (
            parse_decimal(market.get("direct_market_cap")) if market else None
        )

        # ETFs and test issues are conclusively excluded by the source record;
        # their absence from the candidate-only market snapshot is expected.
        source_excluded = bool(reasons)

        if not source_excluded:
            if market is None:
                reasons.append("missing_market_data")
            else:
                if not acquisition_status:
                    reasons.append("missing_acquisition_status")
                elif acquisition_status != "quote":
                    reasons.append(f"acquisition_{acquisition_status}")

                if volume is None:
                    reasons.append("missing_volume")
                elif volume < config.minimum_volume:
                    reasons.append("volume_below_min")

                if close is None:
                    reasons.append("missing_close")
                elif close < config.minimum_close:
                    reasons.append("close_below_min")

                if not instrument_status:
                    reasons.append("missing_instrument_status")
                elif instrument_status != "returned":
                    reasons.append("missing_instrument")
                elif market_cap is None:
                    reasons.append("missing_direct_market_cap")
                elif market_cap < config.minimum_market_cap:
                    reasons.append("market_cap_below_min")
                elif market_cap > config.maximum_market_cap:
                    reasons.append("market_cap_above_max")

        if reasons:
            decision = "reject"
            primary_reason = reasons[0]
            reason_codes = ";".join(reasons)
        else:
            decision = "include"
            primary_reason = "included"
            reason_codes = "included"

        decisions.append(
            UniverseDecision(
                symbol=symbol,
                decision=decision,
                primary_reason=primary_reason,
                reason_codes=reason_codes,
                security_name=_source_value(source, market, "security_name"),
                nasdaq_source=_source_value(
                    source, market, "source", "nasdaq_source"
                ),
                listing_exchange_code=_source_value(
                    source, market, "listing_exchange_code"
                ),
                listing_exchange_name=_source_value(
                    source, market, "listing_exchange_name"
                ),
                market_category=_source_value(
                    source, market, "market_category"
                ),
                financial_status=_source_value(
                    source, market, "financial_status"
                ),
                etf=_source_value(source, market, "etf"),
                test_issue=_source_value(source, market, "test_issue"),
                acquisition_status=acquisition_status,
                acquisition_detail=acquisition_detail,
                close_price=decimal_text(close),
                total_volume=decimal_text(volume),
                instrument_status=instrument_status,
                direct_market_cap=decimal_text(market_cap),
            )
        )

    return tuple(decisions)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_csv(
    path: Path,
    *,
    fieldnames: list[str],
    rows: Iterable[Mapping[str, Any]],
) -> None:
    with path.open("x", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_daily_universe_artifacts(
    *,
    output_dir: str | Path,
    decisions: Iterable[UniverseDecision],
    symbol_directory_path: str | Path,
    market_data_path: str | Path,
    session_date: date,
    target_date: date,
    config: UniverseFilterConfig | None = None,
    generated_at_utc: datetime | None = None,
) -> dict[str, Path]:
    """Create an immutable decision ledger, watchlist, and manifest."""

    if target_date <= session_date:
        raise ValueError("target_date must be after session_date")

    config = config or UniverseFilterConfig()
    decision_rows = tuple(sorted(decisions, key=lambda decision: decision.symbol))
    decision_symbols = [decision.symbol for decision in decision_rows]
    if len(decision_symbols) != len(set(decision_symbols)):
        raise ValueError("decisions contain duplicate symbols")
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=False)

    ledger_path = destination / "decision_ledger.csv"
    watchlist_path = destination / "uni_watchlist.csv"
    symbols_path = destination / "uni_symbols.csv"
    manifest_path = destination / "manifest.json"

    _write_csv(
        ledger_path,
        fieldnames=DECISION_LEDGER_FIELDS,
        rows=(asdict(decision) for decision in decision_rows),
    )

    included = [decision.symbol for decision in decision_rows if decision.included]

    _write_csv(
        watchlist_path,
        fieldnames=["Symbol", "OV_DECISION"],
        rows=({"Symbol": symbol, "OV_DECISION": ""} for symbol in included),
    )
    _write_csv(
        symbols_path,
        fieldnames=["symbol"],
        rows=({"symbol": symbol} for symbol in included),
    )

    generated_at = generated_at_utc or datetime.now(timezone.utc)
    if generated_at.tzinfo is None:
        raise ValueError("generated_at_utc must be timezone-aware")
    generated_at = generated_at.astimezone(timezone.utc)

    reason_counts: Counter[str] = Counter()
    for decision in decision_rows:
        reason_counts.update(decision.reason_codes.split(";"))

    source_path = Path(symbol_directory_path)
    market_path = Path(market_data_path)
    manifest = {
        "selector_version": SELECTOR_VERSION,
        "generated_at_utc": generated_at.isoformat().replace("+00:00", "Z"),
        "session_date": session_date.isoformat(),
        "target_date": target_date.isoformat(),
        "inputs": {
            "symbol_directory": {
                "path": str(source_path),
                "sha256": sha256_file(source_path),
            },
            "market_data_snapshot": {
                "path": str(market_path),
                "sha256": sha256_file(market_path),
            },
        },
        "filters": {
            "minimum_volume": decimal_text(config.minimum_volume),
            "minimum_close": decimal_text(config.minimum_close),
            "minimum_market_cap": decimal_text(config.minimum_market_cap),
            "maximum_market_cap": decimal_text(config.maximum_market_cap),
            "market_cap_source": "Schwab Instruments fundamental.marketCap",
        },
        "counts": {
            "source_symbols": len(decision_rows),
            "included": len(included),
            "rejected": len(decision_rows) - len(included),
        },
        "reason_counts": dict(sorted(reason_counts.items())),
        "artifacts": {
            "decision_ledger": {
                "path": ledger_path.name,
                "sha256": sha256_file(ledger_path),
            },
            "uni_watchlist": {
                "path": watchlist_path.name,
                "sha256": sha256_file(watchlist_path),
            },
            "uni_symbols": {
                "path": symbols_path.name,
                "sha256": sha256_file(symbols_path),
            },
        },
    }

    with manifest_path.open("x", encoding="utf-8") as file:
        json.dump(manifest, file, indent=2, sort_keys=True)
        file.write("\n")

    return {
        "decision_ledger": ledger_path,
        "uni_watchlist": watchlist_path,
        "uni_symbols": symbols_path,
        "manifest": manifest_path,
    }
