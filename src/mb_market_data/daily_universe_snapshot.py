"""Build a frozen post-close selector input from batched Schwab quotes."""

from __future__ import annotations

import csv
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mb_market_data.daily_universe import (
    decimal_text,
    index_unique_rows,
    normalize_symbol,
    parse_decimal,
    sha256_file,
)
from mb_market_data.schwab_quotes import QuoteBatchResult


ET = ZoneInfo("America/New_York")
SNAPSHOT_VERSION = "daily-universe-snapshot-v2"
ACQUISITION_TIMING_POLICY = "same-et-date-post-close-v1"
VOLUME_SOURCE = "Schwab quote.totalVolume"
VOLUME_SEMANTICS = (
    "Current-session cumulative volume accepted as completed-session "
    "evidence only when acquired on session_date at or after 16:00 ET"
)

SOURCE_FIELDS = [
    "symbol",
    "security_name",
    "source",
    "listing_exchange_code",
    "listing_exchange_name",
    "market_category",
    "test_issue",
    "financial_status",
    "etf",
    "round_lot_size",
    "cqs_symbol",
    "nasdaq_symbol",
]

ACQUISITION_FIELDS = [
    "acquisition_status",
    "acquisition_detail",
    "batch_number",
    "request_started_at_utc",
    "response_received_at_utc",
    "regular_market_trade_time_et",
    "regular_market_session_match",
    "close_price",
    "total_volume",
    "shares_outstanding",
    "calculated_market_cap",
    "direct_market_cap",
    "quote_close_price",
    "quote_last_price",
    "asset_main_type",
    "asset_sub_type",
]

SNAPSHOT_FIELDS = SOURCE_FIELDS + ACQUISITION_FIELDS


def nested(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def utc_text(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def epoch_ms_to_et(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, tz=ET)
    except (OSError, OverflowError, ValueError):
        return None


def validate_acquisition_time(
    session_date: date,
    observed_at: datetime,
) -> None:
    """Require quote.totalVolume acquisition in the same-day close window."""

    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    observed_et = observed_at.astimezone(ET)
    if session_date > observed_et.date():
        raise ValueError("session_date must not be in the future")
    if session_date < observed_et.date():
        raise ValueError(
            "daily-universe quote.totalVolume acquisition must run on "
            "session_date; next-day snapshots are not valid "
            "completed-session volume evidence"
        )
    if observed_et.time().replace(tzinfo=None) < time(16, 0):
        raise ValueError(
            "same-day daily-universe acquisition must run at or after "
            "16:00 ET"
        )


def validate_batch_acquisition_time(
    session_date: date,
    acquisition: QuoteBatchResult,
) -> None:
    """Validate every actual request boundary before writing artifacts."""

    if not acquisition.results:
        raise ValueError("quote acquisition returned no timestamped results")
    observed_times = {
        result.request_started_at_utc
        for result in acquisition.results
    }
    observed_times.update(
        result.response_received_at_utc
        for result in acquisition.results
        if result.response_received_at_utc is not None
    )
    for observed_at in sorted(observed_times):
        validate_acquisition_time(session_date, observed_at)


def acquisition_time_bounds(
    acquisition: QuoteBatchResult,
) -> tuple[datetime | None, datetime | None]:
    """Return the first request and last recorded response in UTC."""

    request_times = [
        result.request_started_at_utc
        for result in acquisition.results
    ]
    response_times = [
        result.response_received_at_utc
        for result in acquisition.results
        if result.response_received_at_utc is not None
    ]
    return (
        min(request_times) if request_times else None,
        max(response_times) if response_times else None,
    )


def build_market_data_snapshot(
    candidate_rows: Iterable[Mapping[str, Any]],
    acquisition: QuoteBatchResult,
    *,
    session_date: date,
) -> tuple[dict[str, str], ...]:
    """Normalize one batched post-close acquisition into selector rows."""

    candidates = index_unique_rows(
        candidate_rows,
        input_name="candidate universe",
    )
    results = acquisition.by_symbol()

    missing = sorted(set(candidates) - set(results))
    unexpected = sorted(set(results) - set(candidates))
    if missing or unexpected:
        raise ValueError(
            "quote results do not match candidates: "
            f"missing={missing[:10]!r}, unexpected={unexpected[:10]!r}"
        )

    rows: list[dict[str, str]] = []

    for symbol in sorted(candidates):
        source = candidates[symbol]
        result = results[symbol]
        quote = result.quote or {}

        close = parse_decimal(
            nested(quote, "regular", "regularMarketLastPrice")
        )
        volume = parse_decimal(nested(quote, "quote", "totalVolume"))
        shares = parse_decimal(
            nested(quote, "fundamental", "sharesOutstanding")
        )
        direct_market_cap = parse_decimal(
            nested(quote, "fundamental", "marketCap")
        )
        calculated_market_cap: Decimal | None = None
        if close is not None and shares is not None:
            calculated_market_cap = close * shares

        trade_time = epoch_ms_to_et(
            nested(quote, "regular", "regularMarketTradeTime")
        )
        if trade_time is None:
            session_match = "missing"
        else:
            session_match = (
                "true" if trade_time.date() == session_date else "false"
            )

        row = {
            field: str(source.get(field) or "").strip()
            for field in SOURCE_FIELDS
        }
        row["symbol"] = symbol
        row.update(
            {
                "acquisition_status": result.status.value,
                "acquisition_detail": result.detail or "",
                "batch_number": str(result.batch_number),
                "request_started_at_utc": utc_text(
                    result.request_started_at_utc
                ),
                "response_received_at_utc": utc_text(
                    result.response_received_at_utc
                ),
                "regular_market_trade_time_et": (
                    trade_time.isoformat() if trade_time is not None else ""
                ),
                "regular_market_session_match": session_match,
                "close_price": decimal_text(close),
                "total_volume": decimal_text(volume),
                "shares_outstanding": decimal_text(shares),
                "calculated_market_cap": decimal_text(
                    calculated_market_cap
                ),
                "direct_market_cap": decimal_text(direct_market_cap),
                "quote_close_price": decimal_text(
                    parse_decimal(nested(quote, "quote", "closePrice"))
                ),
                "quote_last_price": decimal_text(
                    parse_decimal(nested(quote, "quote", "lastPrice"))
                ),
                "asset_main_type": str(
                    quote.get("assetMainType") or ""
                ).strip(),
                "asset_sub_type": str(
                    quote.get("assetSubType") or ""
                ).strip(),
            }
        )
        rows.append(row)

    return tuple(rows)


def write_snapshot_csv(
    path: str | Path,
    rows: Iterable[Mapping[str, Any]],
) -> None:
    with Path(path).open("x", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=SNAPSHOT_FIELDS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def write_acquisition_json(
    path: str | Path,
    acquisition: QuoteBatchResult,
    *,
    session_date: date,
) -> None:
    payload = {
        "snapshot_version": SNAPSHOT_VERSION,
        "session_date": session_date.isoformat(),
        "request_count": acquisition.request_count,
        "batch_size": acquisition.batch_size,
        "unexpected_symbols": list(acquisition.unexpected_symbols),
        "results": [
            {
                "symbol": result.symbol,
                "status": result.status.value,
                "detail": result.detail,
                "batch_number": result.batch_number,
                "request_started_at_utc": utc_text(
                    result.request_started_at_utc
                ),
                "response_received_at_utc": utc_text(
                    result.response_received_at_utc
                ),
                "quote": result.quote,
            }
            for result in acquisition.results
        ],
    }
    with Path(path).open("x", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, sort_keys=True)
        file.write("\n")


def write_snapshot_manifest(
    path: str | Path,
    *,
    session_date: date,
    candidate_path: str | Path,
    snapshot_path: str | Path,
    acquisition_path: str | Path,
    acquisition: QuoteBatchResult,
    rows: Iterable[Mapping[str, str]],
    validated_at: datetime,
    created_at_utc: datetime | None = None,
) -> None:
    validate_acquisition_time(session_date, validated_at)
    validate_batch_acquisition_time(session_date, acquisition)
    created_at = created_at_utc or datetime.now(timezone.utc)
    first_request, last_response = acquisition_time_bounds(acquisition)
    row_values = tuple(rows)
    status_counts = Counter(row["acquisition_status"] for row in row_values)
    session_counts = Counter(
        row["regular_market_session_match"] or "missing"
        for row in row_values
    )
    manifest = {
        "snapshot_version": SNAPSHOT_VERSION,
        "created_at_utc": utc_text(created_at),
        "session_date": session_date.isoformat(),
        "sources": {
            "close": "Schwab regular.regularMarketLastPrice",
            "volume": VOLUME_SOURCE,
            "shares": "Schwab fundamental.sharesOutstanding",
            "market_cap": "completed close * sharesOutstanding",
        },
        "source_semantics": {
            "volume": VOLUME_SEMANTICS,
        },
        "input": {
            "path": str(candidate_path),
            "sha256": sha256_file(candidate_path),
        },
        "acquisition": {
            "timing_policy": ACQUISITION_TIMING_POLICY,
            "validated_at_utc": utc_text(validated_at),
            "validated_at_et": validated_at.astimezone(ET).isoformat(),
            "first_request_started_at_utc": utc_text(first_request),
            "last_response_received_at_utc": utc_text(last_response),
            "request_count": acquisition.request_count,
            "batch_size": acquisition.batch_size,
            "unexpected_symbols": list(acquisition.unexpected_symbols),
            "status_counts": dict(sorted(status_counts.items())),
            "regular_session_match_counts": dict(
                sorted(session_counts.items())
            ),
        },
        "artifacts": {
            "market_data_snapshot": {
                "path": Path(snapshot_path).name,
                "sha256": sha256_file(snapshot_path),
            },
            "quote_acquisition": {
                "path": Path(acquisition_path).name,
                "sha256": sha256_file(acquisition_path),
            },
        },
    }
    with Path(path).open("x", encoding="utf-8") as file:
        json.dump(manifest, file, indent=2, sort_keys=True)
        file.write("\n")
