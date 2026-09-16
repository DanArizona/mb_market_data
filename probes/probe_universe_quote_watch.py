"""
Watch a full symbol universe using production batched Schwab quote acquisition.

Purpose
-------
Exploratory diagnostic code for the mb_market_data post-POC universe data plane.
The immediate goal is to measure whether Schwab can provide timely, explicit,
repeatable quote observations for the full candidate universe independently of
ThinkOrSwim Watchlist membership, especially around the regular-market open.

The probe:
    - loads the preserved ThinkOrSwim universe CSV by default;
    - optionally accepts an explicit short symbol list for smoke tests;
    - calls mb_market_data.schwab_quotes.fetch_quotes_batched();
    - keeps one explicit result for every requested symbol;
    - records per-batch request/response timing;
    - writes normalized per-symbol quote evidence to CSV;
    - writes one acquisition summary row per sample;
    - preserves each normalized acquisition, including quote payloads, as JSONL;
    - can append scheduled acquisitions to a shared daily SQLite journal;
    - flushes evidence to disk after every sample;
    - can stop automatically at a specified Eastern Time;
    - can run a fixed number of samples for testing.

All human-facing times use America/New_York. Production acquisition timestamps
from schwab_quotes are stored in UTC and are preserved here in both UTC and ET.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import socket
import statistics
import subprocess
import sys
import time as time_module
from collections.abc import Mapping
from datetime import datetime, time as datetime_time, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mb_market_data.polling_journal import (
    PollingJournalSession,
    daily_quote_journal_path,
)
from mb_market_data.quote_observation_store import RecordResult
from mb_market_data.schwab_quotes import (
    DEFAULT_QUOTE_BATCH_SIZE,
    QuoteBatchResult,
    QuoteResult,
    QuoteStatus,
    fetch_quotes_batched,
    normalize_symbols,
)
from mb_market_data.tos_watchlist import read_tos_watchlist
from mb_market_data.watchlist_polling import (
    POLL_SECONDS,
    MembershipUnavailableError,
    PollRequest,
    PollSlotGuard,
    PollWindow,
    SkippedPollSlot,
    WatchlistKind,
    WatchlistSnapshot,
    capture_poll_request,
    resolve_provider_poll_slot,
    next_poll_slot,
)
from mb_tools.schwab_secure import (
    console_auth_callback,
    make_secure_schwab_client,
)


ET = ZoneInfo("America/New_York")

DEFAULT_UNIVERSE_CSV = Path(
    "probes/evidence/2026-08-10-watchlist2.csv"
)


class JournalWriteError(RuntimeError):
    """A completed API acquisition could not be recorded durably."""


QUOTE_CSV_FIELDS = [
    "sample_number",
    "watchlist_kind",
    "watchlist_revision",
    "slot_id",
    "batch_id",
    "scheduled_at_et",
    "dispatch_lateness_seconds",
    "observed_at_et",
    "observed_at_utc",
    "symbol",
    "acquisition_status",
    "acquisition_detail",
    "batch_number",
    "request_started_at_et",
    "request_started_at_utc",
    "response_received_at_et",
    "response_received_at_utc",
    "batch_duration_seconds",
    "assetMainType",
    "assetSubType",
    "realtime",
    "extended_bidPrice",
    "extended_askPrice",
    "extended_mark",
    "extended_lastPrice",
    "extended_lastSize",
    "extended_totalVolume",
    "extended_quoteTime_ms",
    "extended_quoteTime_et",
    "extended_tradeTime_ms",
    "extended_tradeTime_et",
    "quote_bidPrice",
    "quote_askPrice",
    "quote_mark",
    "quote_lastPrice",
    "quote_lastSize",
    "quote_totalVolume",
    "quote_openPrice",
    "quote_highPrice",
    "quote_lowPrice",
    "quote_closePrice",
    "quote_netChange",
    "quote_netPercentChange",
    "quote_quoteTime_ms",
    "quote_quoteTime_et",
    "quote_quote_age_seconds",
    "quote_tradeTime_ms",
    "quote_tradeTime_et",
    "quote_trade_age_seconds",
    "quote_postMarketChange",
    "quote_postMarketPercentChange",
    "regular_regularMarketLastPrice",
    "regular_regularMarketLastSize",
    "regular_regularMarketTradeTime_ms",
    "regular_regularMarketTradeTime_et",
    "regular_regularMarketNetChange",
    "regular_regularMarketPercentChange",
    "fundamental_sharesOutstanding",
    "reference_cusip",
    "reference_description",
    "reference_exchange",
    "reference_exchangeName",
]

SUMMARY_CSV_FIELDS = [
    "sample_number",
    "watchlist_kind",
    "watchlist_revision",
    "slot_id",
    "batch_id",
    "scheduled_at_et",
    "dispatch_lateness_seconds",
    "sample_started_at_et",
    "sample_completed_at_et",
    "sample_elapsed_seconds",
    "journal_write_seconds",
    "sample_end_to_end_seconds",
    "input_symbols",
    "results",
    "http_requests",
    "quote_count",
    "invalid_count",
    "missing_count",
    "request_error_count",
    "unexpected_symbol_count",
    "comparable_quote_count",
    "volume_increased_count",
    "volume_unchanged_count",
    "trade_time_advanced_count",
    "trade_time_unchanged_count",
    "quote_age_count",
    "quote_age_median_seconds",
    "quote_age_p95_seconds",
    "quote_age_max_seconds",
    "quote_age_over_60s_count",
    "trade_age_count",
    "trade_age_median_seconds",
    "trade_age_p95_seconds",
    "trade_age_max_seconds",
    "trade_age_over_60s_count",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Watch the full candidate universe using batched Schwab quotes."
        )
    )
    parser.add_argument(
        "--universe-csv",
        default=str(DEFAULT_UNIVERSE_CSV),
        help=(
            "ThinkOrSwim Watchlist CSV containing the universe. "
            f"Default: {DEFAULT_UNIVERSE_CSV}"
        ),
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        help=(
            "Explicit symbols for a short smoke test. When supplied, "
            "--universe-csv is not read."
        ),
    )
    parser.add_argument(
        "--fields",
        choices=["quote", "fundamental", "all"],
        default="all",
        help="Schwab quote fields to request. Default: all",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_QUOTE_BATCH_SIZE,
        help=(
            "Symbols per Schwab request. Default: "
            f"{DEFAULT_QUOTE_BATCH_SIZE}"
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Seconds between sample starts. Default: 60",
    )
    parser.add_argument(
        "--watchlist-kind",
        choices=[kind.value for kind in WatchlistKind],
        help=(
            "Use exact wall-clock polling slots for this Watchlist: "
            "uni=:00/:30; focus=:05/:20/:35/:50. When omitted, "
            "the legacy --interval loop is used. Run Uni and Focus "
            "as separate processes so their polling remains independent."
        ),
    )
    parser.add_argument(
        "--watchlist-revision",
        type=int,
        default=0,
        help=(
            "Coordinator membership revision recorded with every sample. "
            "Default: 0 (static probe input)."
        ),
    )
    parser.add_argument(
        "--stop-at",
        help=(
            "Automatically stop at this Eastern Time. "
            "Format: YYYY-MM-DDTHH:MM or YYYY-MM-DDTHH:MM:SS"
        ),
    )
    parser.add_argument(
        "--max-samples",
        type=int,
        help="Stop after this many samples. Useful for short tests.",
    )
    parser.add_argument(
        "--ecfg",
        help="Explicit path to secure_schwabdev.ecfg.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=20,
        help="Schwab request timeout in seconds. Default: 20",
    )
    parser.add_argument(
        "--journal-root",
        help=(
            "Opt in to the shared daily SQLite quote journal. The probe "
            "creates YYYY-MM-DD.sqlite3 below this directory. Requires "
            "--watchlist-kind."
        ),
    )
    parser.add_argument(
        "--journal-schema-version",
        type=int,
        choices=(1, 2),
        default=1,
        help=(
            "Journal contract to use. Version 1 records static startup "
            "membership; version 2 resolves an already-published atomic "
            "hierarchy at every slot. Default: 1"
        ),
    )
    parser.add_argument(
        "--output-root",
        default="output/universe_quote_watch",
        help=(
            "Root directory for timestamped run folders. "
            "Default: output/universe_quote_watch"
        ),
    )
    return parser.parse_args()


def resolve_ecfg(explicit_path: str | None) -> Path:
    candidates: list[Path] = []

    if explicit_path:
        candidates.append(Path(explicit_path).expanduser())

    env_ecfg = os.environ.get("MB_SCHWAB_ECFG")
    if env_ecfg:
        candidates.append(Path(env_ecfg).expanduser())

    mb_vault = os.environ.get("MB_VAULT")
    if mb_vault:
        candidates.append(
            Path(mb_vault).expanduser() / "secure_schwabdev.ecfg"
        )

    candidates.append(Path.cwd() / "secure_schwabdev.ecfg")

    for path in candidates:
        if path.is_file():
            return path.resolve()

    searched = "\n".join(f"  {path}" for path in candidates)
    raise FileNotFoundError(
        "Could not find secure_schwabdev.ecfg.\n"
        "Paths checked:\n"
        f"{searched}"
    )


def parse_et_datetime(text: str | None) -> datetime | None:
    if text is None:
        return None

    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SystemExit(
            f"Invalid datetime {text!r}. "
            "Expected YYYY-MM-DDTHH:MM"
        ) from exc

    if dt.tzinfo is None:
        return dt.replace(tzinfo=ET)

    return dt.astimezone(ET)


def load_symbols(args: argparse.Namespace) -> tuple[str, ...]:
    if args.symbols:
        symbols = normalize_symbols(args.symbols)
        if not symbols:
            raise SystemExit("No valid --symbols were supplied.")
        return symbols

    universe_path = Path(args.universe_csv).expanduser()
    if not universe_path.is_file():
        raise FileNotFoundError(
            f"Universe CSV does not exist: {universe_path}"
        )

    watchlist = read_tos_watchlist(universe_path)
    symbols = normalize_symbols(row.symbol for row in watchlist.rows)

    if not symbols:
        raise SystemExit(
            f"No symbols were found in universe CSV: {universe_path}"
        )

    return symbols


def nested(value: Mapping[str, Any], *keys: str) -> Any:
    current: Any = value

    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
        if current is None:
            return None

    return current


def epoch_ms_to_et_string(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None

    try:
        return datetime.fromtimestamp(
            value / 1000.0,
            tz=ET,
        ).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def epoch_ms_age_seconds(
    value: Any,
    observed_at: datetime,
) -> float | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None

    try:
        event_dt = datetime.fromtimestamp(
            value / 1000.0,
            tz=timezone.utc,
        )
    except (OSError, OverflowError, ValueError):
        return None

    return round(
        (
            observed_at.astimezone(timezone.utc)
            - event_dt
        ).total_seconds(),
        3,
    )


def iso_et(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(ET).isoformat()


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def batch_duration_seconds(item: QuoteResult) -> float | None:
    if item.response_received_at_utc is None:
        return None

    return round(
        (
            item.response_received_at_utc
            - item.request_started_at_utc
        ).total_seconds(),
        6,
    )


def poll_request_fields(
    poll_request: PollRequest | None,
) -> dict[str, Any]:
    """Return stable CSV/JSON fields for optional exact-slot polling."""

    if poll_request is None:
        return {
            "watchlist_kind": None,
            "watchlist_revision": None,
            "slot_id": None,
            "batch_id": None,
            "scheduled_at_et": None,
            "dispatch_lateness_seconds": None,
        }

    return {
        "watchlist_kind": poll_request.watchlist_kind.value,
        "watchlist_revision": poll_request.watchlist_revision,
        "slot_id": poll_request.slot_id,
        "batch_id": poll_request.batch_id,
        "scheduled_at_et": poll_request.slot.scheduled_at.astimezone(
            ET
        ).isoformat(),
        "dispatch_lateness_seconds": round(
            poll_request.dispatch_lateness_seconds,
            6,
        ),
    }


def quote_csv_row(
    *,
    sample_number: int,
    observed_at: datetime,
    item: QuoteResult,
    poll_request: PollRequest | None = None,
) -> dict[str, Any]:
    quote: Mapping[str, Any] = item.quote or {}

    ext_quote_time = nested(quote, "extended", "quoteTime")
    ext_trade_time = nested(quote, "extended", "tradeTime")
    quote_quote_time = nested(quote, "quote", "quoteTime")
    quote_trade_time = nested(quote, "quote", "tradeTime")
    regular_trade_time = nested(
        quote,
        "regular",
        "regularMarketTradeTime",
    )

    return {
        "sample_number": sample_number,
        **poll_request_fields(poll_request),
        "observed_at_et": observed_at.isoformat(),
        "observed_at_utc": observed_at.astimezone(
            timezone.utc
        ).isoformat(),
        "symbol": item.symbol,
        "acquisition_status": item.status.value,
        "acquisition_detail": item.detail,
        "batch_number": item.batch_number,
        "request_started_at_et": iso_et(
            item.request_started_at_utc
        ),
        "request_started_at_utc": iso_utc(
            item.request_started_at_utc
        ),
        "response_received_at_et": iso_et(
            item.response_received_at_utc
        ),
        "response_received_at_utc": iso_utc(
            item.response_received_at_utc
        ),
        "batch_duration_seconds": batch_duration_seconds(item),
        "assetMainType": quote.get("assetMainType"),
        "assetSubType": quote.get("assetSubType"),
        "realtime": quote.get("realtime"),
        "extended_bidPrice": nested(
            quote, "extended", "bidPrice"
        ),
        "extended_askPrice": nested(
            quote, "extended", "askPrice"
        ),
        "extended_mark": nested(quote, "extended", "mark"),
        "extended_lastPrice": nested(
            quote, "extended", "lastPrice"
        ),
        "extended_lastSize": nested(
            quote, "extended", "lastSize"
        ),
        "extended_totalVolume": nested(
            quote, "extended", "totalVolume"
        ),
        "extended_quoteTime_ms": ext_quote_time,
        "extended_quoteTime_et": epoch_ms_to_et_string(
            ext_quote_time
        ),
        "extended_tradeTime_ms": ext_trade_time,
        "extended_tradeTime_et": epoch_ms_to_et_string(
            ext_trade_time
        ),
        "quote_bidPrice": nested(quote, "quote", "bidPrice"),
        "quote_askPrice": nested(quote, "quote", "askPrice"),
        "quote_mark": nested(quote, "quote", "mark"),
        "quote_lastPrice": nested(
            quote, "quote", "lastPrice"
        ),
        "quote_lastSize": nested(quote, "quote", "lastSize"),
        "quote_totalVolume": nested(
            quote, "quote", "totalVolume"
        ),
        "quote_openPrice": nested(quote, "quote", "openPrice"),
        "quote_highPrice": nested(quote, "quote", "highPrice"),
        "quote_lowPrice": nested(quote, "quote", "lowPrice"),
        "quote_closePrice": nested(quote, "quote", "closePrice"),
        "quote_netChange": nested(quote, "quote", "netChange"),
        "quote_netPercentChange": nested(
            quote, "quote", "netPercentChange"
        ),
        "quote_quoteTime_ms": quote_quote_time,
        "quote_quoteTime_et": epoch_ms_to_et_string(
            quote_quote_time
        ),
        "quote_quote_age_seconds": epoch_ms_age_seconds(
            quote_quote_time,
            observed_at,
        ),
        "quote_tradeTime_ms": quote_trade_time,
        "quote_tradeTime_et": epoch_ms_to_et_string(
            quote_trade_time
        ),
        "quote_trade_age_seconds": epoch_ms_age_seconds(
            quote_trade_time,
            observed_at,
        ),
        "quote_postMarketChange": nested(
            quote, "quote", "postMarketChange"
        ),
        "quote_postMarketPercentChange": nested(
            quote, "quote", "postMarketPercentChange"
        ),
        "regular_regularMarketLastPrice": nested(
            quote, "regular", "regularMarketLastPrice"
        ),
        "regular_regularMarketLastSize": nested(
            quote, "regular", "regularMarketLastSize"
        ),
        "regular_regularMarketTradeTime_ms": regular_trade_time,
        "regular_regularMarketTradeTime_et": epoch_ms_to_et_string(
            regular_trade_time
        ),
        "regular_regularMarketNetChange": nested(
            quote, "regular", "regularMarketNetChange"
        ),
        "regular_regularMarketPercentChange": nested(
            quote, "regular", "regularMarketPercentChange"
        ),
        "fundamental_sharesOutstanding": nested(
            quote, "fundamental", "sharesOutstanding"
        ),
        "reference_cusip": nested(
            quote, "reference", "cusip"
        ),
        "reference_description": nested(
            quote, "reference", "description"
        ),
        "reference_exchange": nested(
            quote, "reference", "exchange"
        ),
        "reference_exchangeName": nested(
            quote, "reference", "exchangeName"
        ),
    }


def force_flush(file: Any) -> None:
    file.flush()
    try:
        os.fsync(file.fileno())
    except OSError:
        pass


def build_batch_summaries(
    result: QuoteBatchResult,
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    batch_numbers = sorted(
        {item.batch_number for item in result.results}
    )

    for batch_number in batch_numbers:
        items = [
            item
            for item in result.results
            if item.batch_number == batch_number
        ]
        if not items:
            continue

        first = items[0]
        counts = {
            status.value: sum(
                1 for item in items if item.status == status
            )
            for status in QuoteStatus
        }
        summaries.append(
            {
                "batch_number": batch_number,
                "symbols": len(items),
                "request_started_at_et": iso_et(
                    first.request_started_at_utc
                ),
                "request_started_at_utc": iso_utc(
                    first.request_started_at_utc
                ),
                "response_received_at_et": iso_et(
                    first.response_received_at_utc
                ),
                "response_received_at_utc": iso_utc(
                    first.response_received_at_utc
                ),
                "duration_seconds": batch_duration_seconds(first),
                "status_counts": counts,
            }
        )

    return summaries


def normalized_acquisition_record(
    *,
    sample_number: int,
    sample_started_at: datetime,
    sample_completed_at: datetime,
    result: QuoteBatchResult,
    poll_request: PollRequest | None = None,
) -> dict[str, Any]:
    return {
        "sample_number": sample_number,
        **poll_request_fields(poll_request),
        "sample_started_at_et": sample_started_at.isoformat(),
        "sample_started_at_utc": sample_started_at.astimezone(
            timezone.utc
        ).isoformat(),
        "sample_completed_at_et": sample_completed_at.isoformat(),
        "sample_completed_at_utc": sample_completed_at.astimezone(
            timezone.utc
        ).isoformat(),
        "request_count": result.request_count,
        "batch_size": result.batch_size,
        "unexpected_symbols": list(result.unexpected_symbols),
        "batch_summaries": build_batch_summaries(result),
        "results": [
            {
                "symbol": item.symbol,
                "status": item.status.value,
                "detail": item.detail,
                "batch_number": item.batch_number,
                "request_started_at_utc": iso_utc(
                    item.request_started_at_utc
                ),
                "response_received_at_utc": iso_utc(
                    item.response_received_at_utc
                ),
                "quote": (
                    dict(item.quote)
                    if item.quote is not None
                    else None
                ),
            }
            for item in result.results
        ],
    }


def previous_state_metrics(
    result: QuoteBatchResult,
    previous: dict[str, tuple[Any, Any]],
) -> dict[str, int]:
    comparable = 0
    volume_increased = 0
    volume_unchanged = 0
    trade_time_advanced = 0
    trade_time_unchanged = 0

    next_previous: dict[str, tuple[Any, Any]] = {}

    for item in result.results:
        if item.status != QuoteStatus.QUOTE or item.quote is None:
            continue

        volume = nested(item.quote, "quote", "totalVolume")
        trade_time = nested(item.quote, "quote", "tradeTime")

        old = previous.get(item.symbol)
        if old is not None:
            old_volume, old_trade_time = old
            comparable += 1

            if isinstance(volume, (int, float)) and isinstance(
                old_volume, (int, float)
            ):
                if volume > old_volume:
                    volume_increased += 1
                elif volume == old_volume:
                    volume_unchanged += 1

            if isinstance(trade_time, (int, float)) and isinstance(
                old_trade_time, (int, float)
            ):
                if trade_time > old_trade_time:
                    trade_time_advanced += 1
                elif trade_time == old_trade_time:
                    trade_time_unchanged += 1

        next_previous[item.symbol] = (volume, trade_time)

    previous.clear()
    previous.update(next_previous)

    return {
        "comparable_quote_count": comparable,
        "volume_increased_count": volume_increased,
        "volume_unchanged_count": volume_unchanged,
        "trade_time_advanced_count": trade_time_advanced,
        "trade_time_unchanged_count": trade_time_unchanged,
    }


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None

    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]

    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def duration_summary(values: list[float]) -> dict[str, Any] | None:
    """Return compact latency statistics for a completed probe run."""

    if not values:
        return None
    return {
        "count": len(values),
        "median": round(statistics.median(values), 6),
        "p95": round(percentile(values, 0.95), 6),
        "p99": round(percentile(values, 0.99), 6),
        "max": round(max(values), 6),
    }


def age_summary(values: list[float], prefix: str) -> dict[str, Any]:
    if not values:
        return {
            f"{prefix}_age_count": 0,
            f"{prefix}_age_median_seconds": None,
            f"{prefix}_age_p95_seconds": None,
            f"{prefix}_age_max_seconds": None,
            f"{prefix}_age_over_60s_count": 0,
        }

    return {
        f"{prefix}_age_count": len(values),
        f"{prefix}_age_median_seconds": round(statistics.median(values), 3),
        f"{prefix}_age_p95_seconds": round(percentile(values, 0.95), 3),
        f"{prefix}_age_max_seconds": round(max(values), 3),
        f"{prefix}_age_over_60s_count": sum(value > 60.0 for value in values),
    }


def freshness_metrics(
    result: QuoteBatchResult,
    observed_at: datetime,
) -> dict[str, Any]:
    quote_ages: list[float] = []
    trade_ages: list[float] = []

    for item in result.results:
        if item.status != QuoteStatus.QUOTE or item.quote is None:
            continue

        quote_time = nested(item.quote, "quote", "quoteTime")
        trade_time = nested(item.quote, "quote", "tradeTime")

        quote_age = epoch_ms_age_seconds(quote_time, observed_at)
        trade_age = epoch_ms_age_seconds(trade_time, observed_at)

        if quote_age is not None:
            quote_ages.append(quote_age)
        if trade_age is not None:
            trade_ages.append(trade_age)

    return {
        **age_summary(quote_ages, "quote"),
        **age_summary(trade_ages, "trade"),
    }


def summary_row(
    *,
    sample_number: int,
    sample_started_at: datetime,
    sample_completed_at: datetime,
    input_symbol_count: int,
    result: QuoteBatchResult,
    change_metrics: dict[str, int],
    poll_request: PollRequest | None = None,
) -> dict[str, Any]:
    counts = result.status_counts()

    return {
        "sample_number": sample_number,
        **poll_request_fields(poll_request),
        "sample_started_at_et": sample_started_at.isoformat(),
        "sample_completed_at_et": sample_completed_at.isoformat(),
        "sample_elapsed_seconds": round(
            (sample_completed_at - sample_started_at).total_seconds(),
            6,
        ),
        "input_symbols": input_symbol_count,
        "results": len(result.results),
        "http_requests": result.request_count,
        "quote_count": counts[QuoteStatus.QUOTE],
        "invalid_count": counts[QuoteStatus.INVALID],
        "missing_count": counts[QuoteStatus.MISSING],
        "request_error_count": counts[QuoteStatus.REQUEST_ERROR],
        "unexpected_symbol_count": len(result.unexpected_symbols),
        **change_metrics,
        **freshness_metrics(result, sample_completed_at),
    }


def print_sample_summary(
    row: Mapping[str, Any],
    result: QuoteBatchResult,
) -> None:
    slot_text = ""
    if row.get("slot_id"):
        slot_text = f"  {row['slot_id']}"

    timing_text = ""
    journal_seconds = row.get("journal_write_seconds")
    end_to_end_seconds = row.get("sample_end_to_end_seconds")
    if isinstance(journal_seconds, (int, float)):
        timing_text += f"  journal={journal_seconds:.3f}s"
    if isinstance(end_to_end_seconds, (int, float)):
        timing_text += f"  total={end_to_end_seconds:.3f}s"

    print(
        f"Sample {row['sample_number']:>3}  "
        f"{row['sample_completed_at_et']}  "
        f"elapsed={row['sample_elapsed_seconds']:.3f}s  "
        f"req={row['http_requests']}  "
        f"quote={row['quote_count']}  "
        f"invalid={row['invalid_count']}  "
        f"missing={row['missing_count']}  "
        f"reqerr={row['request_error_count']}  "
        f"vol+={row['volume_increased_count']}  "
        f"trade+={row['trade_time_advanced_count']}"
        f"{slot_text}"
        f"{timing_text}"
    )

    def format_age(value: Any) -> str:
        if isinstance(value, (int, float)):
            return f"{value:.1f}s"
        return "n/a"

    print(
        "  freshness quote: "
        f"n={row['quote_age_count']}  "
        f"med={format_age(row['quote_age_median_seconds'])}  "
        f"p95={format_age(row['quote_age_p95_seconds'])}  "
        f"max={format_age(row['quote_age_max_seconds'])}  "
        f">60s={row['quote_age_over_60s_count']}"
    )
    print(
        "  freshness trade: "
        f"n={row['trade_age_count']}  "
        f"med={format_age(row['trade_age_median_seconds'])}  "
        f"p95={format_age(row['trade_age_p95_seconds'])}  "
        f"max={format_age(row['trade_age_max_seconds'])}  "
        f">60s={row['trade_age_over_60s_count']}"
    )

    unavailable = [
        item
        for item in result.results
        if item.status != QuoteStatus.QUOTE
    ]
    if unavailable:
        preview = " ".join(
            f"{item.symbol}:{item.status.value}"
            for item in unavailable[:20]
        )
        suffix = " ..." if len(unavailable) > 20 else ""
        print(f"  unavailable: {preview}{suffix}")

    for batch in build_batch_summaries(result):
        duration = batch["duration_seconds"]
        duration_text = (
            f"{duration:.3f}s"
            if isinstance(duration, (int, float))
            else "NO RESPONSE"
        )
        print(
            f"  batch {batch['batch_number']}: "
            f"symbols={batch['symbols']}  "
            f"duration={duration_text}"
        )


def write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as file:
        json.dump(
            manifest,
            file,
            indent=2,
            sort_keys=True,
        )


def installed_software_version() -> str:
    """Return package and Git identity when the source checkout is available."""

    try:
        package_version = version("mb-market-data")
    except PackageNotFoundError:
        package_version = "source-tree"

    repository_root = Path(__file__).resolve().parents[1]
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if commit.returncode != 0:
            return package_version

        tracked_status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return package_version

    commit_sha = commit.stdout.strip()
    if not commit_sha:
        return package_version

    dirty_suffix = (
        ".dirty"
        if tracked_status.returncode == 0 and tracked_status.stdout.strip()
        else ""
    )
    return f"{package_version}+git.{commit_sha[:12]}{dirty_suffix}"


def main() -> int:
    args = parse_args()

    if args.interval <= 0:
        raise SystemExit("--interval must be greater than zero.")

    if args.max_samples is not None and args.max_samples <= 0:
        raise SystemExit("--max-samples must be greater than zero.")

    if args.watchlist_revision < 0:
        raise SystemExit("--watchlist-revision must be nonnegative.")

    if args.timeout <= 0:
        raise SystemExit("--timeout must be greater than zero.")

    if args.journal_root and not args.watchlist_kind:
        raise SystemExit(
            "--journal-root requires exact-slot --watchlist-kind polling."
        )

    hierarchy_polling = args.journal_schema_version == 2
    if hierarchy_polling and not args.journal_root:
        raise SystemExit(
            "--journal-schema-version 2 requires --journal-root."
        )
    if hierarchy_polling and args.symbols:
        raise SystemExit(
            "--symbols cannot be used with schema-v2 hierarchy polling; "
            "membership is read from the journal at each slot."
        )
    if hierarchy_polling and args.watchlist_revision != 0:
        raise SystemExit(
            "--watchlist-revision is a schema-v1 static-membership option; "
            "schema-v2 revisions are resolved from the journal."
        )

    symbols = () if hierarchy_polling else load_symbols(args)
    stop_at = parse_et_datetime(args.stop_at)
    ecfg_path = resolve_ecfg(args.ecfg)

    started_at = datetime.now(ET)
    poll_kind = (
        WatchlistKind(args.watchlist_kind)
        if args.watchlist_kind
        else None
    )
    poll_window: PollWindow | None = None
    snapshot: WatchlistSnapshot | None = None
    slot_guard = PollSlotGuard()

    if poll_kind is not None:
        session_start = datetime.combine(
            started_at.date(),
            datetime_time(9, 30),
            tzinfo=ET,
        )
        regular_session_end = datetime.combine(
            started_at.date(),
            datetime_time(16, 0),
            tzinfo=ET,
        )
        session_end = (
            min(stop_at, regular_session_end)
            if stop_at is not None
            else regular_session_end
        )

        if session_end <= session_start:
            raise SystemExit(
                "Exact-slot polling requires --stop-at later than "
                "09:30 ET."
            )

        poll_window = PollWindow(
            start_at=session_start,
            end_at=session_end,
        )
        if not hierarchy_polling:
            snapshot = WatchlistSnapshot(
                watchlist_kind=poll_kind,
                session_date=poll_window.session_date,
                revision=args.watchlist_revision,
                # A static probe revision represents session membership, not
                # this particular process lifetime.  The deterministic
                # effective time makes a same-day restart idempotent.
                effective_at=session_start,
                symbols=symbols,
            )

    run_stamp = started_at.strftime("%Y-%m-%d-%H-%M-%S")
    if poll_kind is not None:
        run_stamp += f"-{poll_kind.value}"
    run_dir = Path(args.output_root) / run_stamp
    run_dir.mkdir(parents=True, exist_ok=True)

    acquisition_path = run_dir / "acquisition_samples.jsonl"
    quote_csv_path = run_dir / "quote_samples.csv"
    summary_csv_path = run_dir / "sample_summary.csv"
    skipped_slot_path = run_dir / "skipped_slots.jsonl"
    error_path = run_dir / "errors.log"
    manifest_path = run_dir / "manifest.json"

    source_text = (
        "schema-v2 journal hierarchy"
        if hierarchy_polling
        else (
            "explicit --symbols"
            if args.symbols
            else str(Path(args.universe_csv).expanduser().resolve())
        )
    )

    journal_session: PollingJournalSession | None = None
    journal_path: Path | None = None
    journal_run_id: str | None = None
    journal_software_version: str | None = None
    if args.journal_root:
        assert poll_kind is not None
        assert poll_window is not None
        journal_path = daily_quote_journal_path(
            Path(args.journal_root).expanduser(),
            poll_window.session_date,
        )
        journal_run_id = (
            "universe_quote_watch:"
            f"{poll_kind.value}:"
            f"{started_at.astimezone(timezone.utc).isoformat(timespec='microseconds')}"
        )
        journal_software_version = installed_software_version()
        journal_configuration = {
            "probe": "universe_quote_watch",
            "watchlist_kind": poll_kind.value,
            "watchlist_revision": (
                None if hierarchy_polling else args.watchlist_revision
            ),
            "membership_source": (
                "journal_hierarchy" if hierarchy_polling else "startup"
            ),
            "symbol_source": source_text,
            "symbol_count": len(symbols) if not hierarchy_polling else None,
            "fields": args.fields,
            "batch_size": args.batch_size,
            "poll_seconds": list(POLL_SECONDS[poll_kind]),
            "poll_window_start_et": poll_window.start_at.isoformat(),
            "poll_window_end_et": poll_window.end_at.isoformat(),
            "evidence_run_directory": str(run_dir.resolve()),
        }
        if hierarchy_polling:
            journal_session = (
                PollingJournalSession.register_hierarchy_polling(
                    journal_path,
                    session_date=poll_window.session_date,
                    run_id=journal_run_id,
                    started_at=started_at,
                    software_version=journal_software_version,
                    configuration=journal_configuration,
                    host=socket.gethostname(),
                    command=tuple(sys.argv),
                )
            )
        else:
            assert snapshot is not None
            journal_session = PollingJournalSession.start(
                journal_path,
                run_id=journal_run_id,
                started_at=started_at,
                software_version=journal_software_version,
                configuration=journal_configuration,
                snapshot=snapshot,
                revision_source=source_text,
                revision_reason="static probe input",
                revision_metadata={"probe": "universe_quote_watch"},
                host=socket.gethostname(),
                command=tuple(sys.argv),
            )

    manifest: dict[str, Any] = {
        "probe": "universe_quote_watch",
        "started_at_et": started_at.isoformat(),
        "symbol_source": source_text,
        "symbol_count": None if hierarchy_polling else len(symbols),
        "symbols": None if hierarchy_polling else list(symbols),
        "fields": args.fields,
        "batch_size": args.batch_size,
        "interval_seconds": args.interval,
        "watchlist_kind": poll_kind.value if poll_kind else None,
        "watchlist_revision": (
            args.watchlist_revision
            if poll_kind and not hierarchy_polling
            else None
        ),
        "poll_seconds": (
            list(POLL_SECONDS[poll_kind]) if poll_kind else None
        ),
        "poll_window_start_et": (
            poll_window.start_at.isoformat() if poll_window else None
        ),
        "poll_window_end_et": (
            poll_window.end_at.isoformat() if poll_window else None
        ),
        "stop_at_et": stop_at.isoformat() if stop_at else None,
        "max_samples": args.max_samples,
        "timeout_seconds": args.timeout,
        "ecfg_path": str(ecfg_path),
        "acquisition_samples_file": str(acquisition_path),
        "quote_csv_file": str(quote_csv_path),
        "summary_csv_file": str(summary_csv_path),
        "skipped_slots_file": str(skipped_slot_path),
        "errors_file": str(error_path),
        "journal_path": str(journal_path) if journal_path else None,
        "journal_schema_version": (
            args.journal_schema_version if journal_path else None
        ),
        "journal_run_id": journal_run_id,
        "journal_software_version": journal_software_version,
        "journal_run_record_result": (
            journal_session.run_record_result.value
            if journal_session
            else None
        ),
        "journal_revision_record_result": (
            journal_session.revision_record_result.value
            if (
                journal_session is not None
                and journal_session.revision_record_result is not None
            )
            else None
        ),
        "journal_acquisitions_inserted": 0,
        "journal_acquisitions_already_present": 0,
        "journal_skips_inserted": 0,
        "journal_skips_already_present": 0,
        "journal_errors": 0,
        "skipped_empty_membership_slots": 0,
        "acquisition_timing_seconds": None,
        "journal_write_timing_seconds": None,
        "end_to_end_timing_seconds": None,
        "completed_at_et": None,
        "samples_attempted": 0,
        "samples_completed": 0,
    }
    write_manifest(manifest_path, manifest)

    print()
    print("Schwab full-universe quote watch")
    print("=" * 79)
    print(f"Symbol source    : {source_text}")
    print(
        "Symbols          : "
        + ("resolved per slot" if hierarchy_polling else str(len(symbols)))
    )
    print(f"Fields           : {args.fields}")
    print(f"Batch size       : {args.batch_size}")
    if poll_kind is None:
        print(f"Interval         : {args.interval:g} seconds")
    else:
        print(f"Watchlist kind   : {poll_kind.value}")
        print(
            "Watchlist rev    : "
            + (
                "resolved per slot"
                if hierarchy_polling
                else str(args.watchlist_revision)
            )
        )
        print(
            "Polling seconds  : "
            + ", ".join(
                f":{second:02d}"
                for second in POLL_SECONDS[poll_kind]
            )
        )
        print(
            "Polling window   : "
            f"{poll_window.start_at:%Y-%m-%d %H:%M:%S %Z} through "
            f"{poll_window.end_at:%H:%M:%S %Z} (end excluded)"
        )
    print(f"Started          : {started_at:%Y-%m-%d %H:%M:%S %Z}")
    print(
        "Automatic stop   : "
        + (
            f"{stop_at:%Y-%m-%d %H:%M:%S %Z}"
            if stop_at
            else "none"
        )
    )
    print(
        "Maximum samples  : "
        + (str(args.max_samples) if args.max_samples else "none")
    )
    print(f"Encrypted config : {ecfg_path}")
    print(f"Output directory : {run_dir}")
    print(
        "Daily journal    : "
        + (str(journal_path) if journal_path else "disabled")
    )
    if journal_path is not None:
        print(f"Journal schema   : {args.journal_schema_version}")
    print()

    password = getpass.getpass("Encrypted config password: ")

    client = None
    sample_number = 0
    completed_count = 0
    journal_inserted_count = 0
    journal_already_present_count = 0
    journal_skip_inserted_count = 0
    journal_skip_already_present_count = 0
    journal_error_count = 0
    skipped_slot_count = 0
    acquisition_durations: list[float] = []
    journal_write_durations: list[float] = []
    end_to_end_durations: list[float] = []
    previous: dict[str, tuple[Any, Any]] = {}

    try:
        client = make_secure_schwab_client(
            ecfg_path,
            password,
            timeout=args.timeout,
            call_on_auth=console_auth_callback,
        )

        with (
            acquisition_path.open("a", encoding="utf-8") as acquisition_file,
            quote_csv_path.open(
                "a", newline="", encoding="utf-8"
            ) as quote_csv_file,
            summary_csv_path.open(
                "a", newline="", encoding="utf-8"
            ) as summary_csv_file,
            skipped_slot_path.open(
                "a", encoding="utf-8"
            ) as skipped_slot_file,
            error_path.open("a", encoding="utf-8") as error_file,
        ):
            quote_writer = csv.DictWriter(
                quote_csv_file,
                fieldnames=QUOTE_CSV_FIELDS,
            )
            summary_writer = csv.DictWriter(
                summary_csv_file,
                fieldnames=SUMMARY_CSV_FIELDS,
            )

            if quote_csv_file.tell() == 0:
                quote_writer.writeheader()
                force_flush(quote_csv_file)

            if summary_csv_file.tell() == 0:
                summary_writer.writeheader()
                force_flush(summary_csv_file)

            next_sample_monotonic = time_module.monotonic()

            while True:
                now = datetime.now(ET)

                if stop_at is not None and now >= stop_at:
                    print("Reached automatic stop time.")
                    break

                if (
                    args.max_samples is not None
                    and sample_number >= args.max_samples
                ):
                    print("Reached maximum sample count.")
                    break

                poll_request: PollRequest | None = None

                if poll_kind is not None:
                    assert poll_window is not None

                    slot = next_poll_slot(
                        poll_kind,
                        after=now,
                        window=poll_window,
                    )
                    if slot is None:
                        print("Reached the end of the polling window.")
                        break

                    while True:
                        delay = (
                            slot.scheduled_at - datetime.now(ET)
                        ).total_seconds()
                        if delay <= 0:
                            break
                        time_module.sleep(delay)

                    dispatched_at = datetime.now(ET)
                    if dispatched_at >= poll_window.end_at:
                        print("Reached the end of the polling window.")
                        break

                    if hierarchy_polling:
                        assert journal_session is not None
                        try:
                            resolved_request = (
                                resolve_provider_poll_slot(
                                    slot,
                                    journal_session,
                                    dispatched_at=dispatched_at,
                                )
                            )
                        except MembershipUnavailableError as exc:
                            message = (
                                f"{dispatched_at.isoformat()} "
                                f"slot={slot.slot_id} "
                                f"{type(exc).__name__}: {exc}\n"
                            )
                            error_file.write(message)
                            force_flush(error_file)
                            journal_error_count += 1
                            print(f"Hierarchy membership ERROR: {exc}")
                            print("Stopping without dispatching the slot.")
                            break

                        if not slot_guard.claim(slot):
                            continue
                        if isinstance(resolved_request, SkippedPollSlot):
                            skipped_slot_file.write(
                                json.dumps(
                                    {
                                        "record_type": "skipped_poll_slot",
                                        "reason": resolved_request.reason,
                                        "slot_id": resolved_request.slot_id,
                                        "watchlist_kind": poll_kind.value,
                                        "watchlist_revision": (
                                            resolved_request.watchlist_revision
                                        ),
                                        "scheduled_at_et": (
                                            slot.scheduled_at.isoformat()
                                        ),
                                        "membership_effective_at_et": (
                                            resolved_request
                                            .membership_effective_at
                                            .astimezone(ET)
                                            .isoformat()
                                        ),
                                        "observed_at_et": (
                                            resolved_request.observed_at
                                            .astimezone(ET)
                                            .isoformat()
                                        ),
                                    },
                                    sort_keys=True,
                                )
                                + "\n"
                            )
                            force_flush(skipped_slot_file)
                            journal_started_monotonic = (
                                time_module.monotonic()
                            )
                            try:
                                skip_result = (
                                    journal_session.record_skipped_slot(
                                        resolved_request
                                    )
                                )
                            except Exception as exc:
                                message = (
                                    f"{datetime.now(ET).isoformat()} "
                                    f"slot={slot.slot_id} "
                                    f"JournalWriteError: Daily journal "
                                    f"skip write failed: {exc}\n"
                                )
                                error_file.write(message)
                                force_flush(error_file)
                                journal_error_count += 1
                                print(
                                    "Daily journal skip write ERROR: "
                                    f"{exc}"
                                )
                                print("Stopping after daily journal failure.")
                                break
                            finally:
                                journal_write_durations.append(
                                    round(
                                        time_module.monotonic()
                                        - journal_started_monotonic,
                                        6,
                                    )
                                )
                            if skip_result == RecordResult.INSERTED:
                                journal_skip_inserted_count += 1
                            else:
                                journal_skip_already_present_count += 1
                            skipped_slot_count += 1
                            print(
                                f"Skipped {slot.slot_id}: "
                                f"{poll_kind.value} membership is empty "
                                f"under r{resolved_request.watchlist_revision}."
                            )
                            continue
                        poll_request = resolved_request
                    else:
                        assert snapshot is not None
                        if not slot_guard.claim(slot):
                            continue
                        poll_request = capture_poll_request(
                            slot,
                            snapshot,
                            dispatched_at=dispatched_at,
                        )

                sample_number += 1
                sample_started_at = (
                    poll_request.dispatched_at
                    if poll_request is not None
                    else datetime.now(ET)
                )
                started_monotonic = time_module.monotonic()
                request_symbols = (
                    poll_request.symbols
                    if poll_request is not None
                    else symbols
                )

                try:
                    result = fetch_quotes_batched(
                        client,
                        request_symbols,
                        fields=args.fields,
                        batch_size=args.batch_size,
                    )
                    sample_completed_at = datetime.now(ET)
                    elapsed_monotonic = (
                        time_module.monotonic() - started_monotonic
                    )

                    acquisition_record = normalized_acquisition_record(
                        sample_number=sample_number,
                        sample_started_at=sample_started_at,
                        sample_completed_at=sample_completed_at,
                        result=result,
                        poll_request=poll_request,
                    )
                    acquisition_record["sample_elapsed_seconds"] = round(
                        elapsed_monotonic,
                        6,
                    )
                    acquisition_file.write(
                        json.dumps(
                            acquisition_record,
                            sort_keys=True,
                        )
                        + "\n"
                    )
                    force_flush(acquisition_file)

                    journal_write_seconds: float | None = None
                    if journal_session is not None:
                        assert poll_request is not None
                        journal_started_monotonic = time_module.monotonic()
                        try:
                            journal_result = (
                                journal_session.record_acquisition(
                                    poll_request,
                                    result,
                                    completed_at=sample_completed_at,
                                )
                            )
                        except Exception as exc:
                            raise JournalWriteError(
                                f"Daily journal write failed for "
                                f"{poll_request.batch_id}: {exc}"
                            ) from exc
                        finally:
                            journal_write_seconds = round(
                                time_module.monotonic()
                                - journal_started_monotonic,
                                6,
                            )

                        if journal_result == RecordResult.INSERTED:
                            journal_inserted_count += 1
                        else:
                            journal_already_present_count += 1

                    for item in result.results:
                        quote_writer.writerow(
                            quote_csv_row(
                                sample_number=sample_number,
                                observed_at=sample_completed_at,
                                item=item,
                                poll_request=poll_request,
                            )
                        )
                    force_flush(quote_csv_file)

                    change_metrics = previous_state_metrics(
                        result,
                        previous,
                    )
                    row = summary_row(
                        sample_number=sample_number,
                        sample_started_at=sample_started_at,
                        sample_completed_at=sample_completed_at,
                        input_symbol_count=len(request_symbols),
                        result=result,
                        change_metrics=change_metrics,
                        poll_request=poll_request,
                    )
                    row["sample_elapsed_seconds"] = round(
                        elapsed_monotonic,
                        6,
                    )
                    row["journal_write_seconds"] = journal_write_seconds
                    end_to_end_seconds = round(
                        time_module.monotonic() - started_monotonic,
                        6,
                    )
                    row["sample_end_to_end_seconds"] = end_to_end_seconds
                    summary_writer.writerow(row)
                    force_flush(summary_csv_file)

                    acquisition_durations.append(elapsed_monotonic)
                    if journal_write_seconds is not None:
                        journal_write_durations.append(
                            journal_write_seconds
                        )
                    end_to_end_durations.append(end_to_end_seconds)
                    completed_count += 1
                    print_sample_summary(row, result)

                except Exception as exc:
                    failed_at = datetime.now(ET)
                    message = (
                        f"{failed_at.isoformat()} "
                        f"sample={sample_number} "
                        f"{type(exc).__name__}: {exc}\n"
                    )
                    error_file.write(message)
                    force_flush(error_file)
                    print(
                        f"Sample {sample_number:>3} ERROR: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    if isinstance(exc, JournalWriteError):
                        journal_error_count += 1
                        print("Stopping after daily journal failure.")
                        break

                if (
                    args.max_samples is not None
                    and sample_number >= args.max_samples
                ):
                    print("Reached maximum sample count.")
                    break

                if poll_kind is None:
                    next_sample_monotonic += args.interval
                    delay = (
                        next_sample_monotonic - time_module.monotonic()
                    )
                    if delay > 0:
                        time_module.sleep(delay)
                    else:
                        next_sample_monotonic = time_module.monotonic()

    except KeyboardInterrupt:
        print()
        print("Stopped by user.")

    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

        completed_at = datetime.now(ET)
        manifest["completed_at_et"] = completed_at.isoformat()
        manifest["samples_attempted"] = sample_number
        manifest["samples_completed"] = completed_count
        manifest["journal_acquisitions_inserted"] = (
            journal_inserted_count
        )
        manifest["journal_acquisitions_already_present"] = (
            journal_already_present_count
        )
        manifest["journal_skips_inserted"] = journal_skip_inserted_count
        manifest["journal_skips_already_present"] = (
            journal_skip_already_present_count
        )
        manifest["journal_errors"] = journal_error_count
        manifest["skipped_empty_membership_slots"] = skipped_slot_count
        manifest["acquisition_timing_seconds"] = duration_summary(
            acquisition_durations
        )
        manifest["journal_write_timing_seconds"] = duration_summary(
            journal_write_durations
        )
        manifest["end_to_end_timing_seconds"] = duration_summary(
            end_to_end_durations
        )
        write_manifest(manifest_path, manifest)

        print()
        print("Universe quote watch finished")
        print("=" * 79)
        print(f"Completed         : {completed_at:%Y-%m-%d %H:%M:%S %Z}")
        print(f"Samples attempted : {sample_number}")
        print(f"Samples completed : {completed_count}")
        if skipped_slot_count:
            print(f"Slots skipped     : {skipped_slot_count}")
        print(f"Acquisitions      : {acquisition_path}")
        print(f"Quote CSV         : {quote_csv_path}")
        print(f"Summary CSV       : {summary_csv_path}")
        if skipped_slot_count:
            print(f"Skipped slots     : {skipped_slot_path}")
        print(f"Errors            : {error_path}")
        print(f"Manifest          : {manifest_path}")
        if journal_path is not None:
            print(f"Daily journal     : {journal_path}")
            print(
                "Journal records   : "
                f"{journal_inserted_count} inserted, "
                f"{journal_already_present_count} already present, "
                f"{journal_error_count} errors"
            )
            if skipped_slot_count:
                print(
                    "Journal skips     : "
                    f"{journal_skip_inserted_count} inserted, "
                    f"{journal_skip_already_present_count} already present"
                )

    return 1 if journal_error_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
