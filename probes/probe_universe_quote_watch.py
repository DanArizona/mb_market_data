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
import statistics
import time as time_module
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mb_market_data.schwab_quotes import (
    DEFAULT_QUOTE_BATCH_SIZE,
    QuoteBatchResult,
    QuoteResult,
    QuoteStatus,
    fetch_quotes_batched,
    normalize_symbols,
)
from mb_market_data.tos_watchlist import read_tos_watchlist
from mb_tools.schwab_secure import (
    console_auth_callback,
    make_secure_schwab_client,
)


ET = ZoneInfo("America/New_York")

DEFAULT_UNIVERSE_CSV = Path(
    "probes/evidence/2026-08-10-watchlist2.csv"
)

QUOTE_CSV_FIELDS = [
    "sample_number",
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
    "sample_started_at_et",
    "sample_completed_at_et",
    "sample_elapsed_seconds",
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


def quote_csv_row(
    *,
    sample_number: int,
    observed_at: datetime,
    item: QuoteResult,
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
) -> dict[str, Any]:
    return {
        "sample_number": sample_number,
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
) -> dict[str, Any]:
    counts = result.status_counts()

    return {
        "sample_number": sample_number,
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


def main() -> int:
    args = parse_args()

    if args.interval <= 0:
        raise SystemExit("--interval must be greater than zero.")

    if args.max_samples is not None and args.max_samples <= 0:
        raise SystemExit("--max-samples must be greater than zero.")

    if args.timeout <= 0:
        raise SystemExit("--timeout must be greater than zero.")

    symbols = load_symbols(args)
    stop_at = parse_et_datetime(args.stop_at)
    ecfg_path = resolve_ecfg(args.ecfg)

    started_at = datetime.now(ET)
    run_stamp = started_at.strftime("%Y-%m-%d-%H-%M-%S")
    run_dir = Path(args.output_root) / run_stamp
    run_dir.mkdir(parents=True, exist_ok=True)

    acquisition_path = run_dir / "acquisition_samples.jsonl"
    quote_csv_path = run_dir / "quote_samples.csv"
    summary_csv_path = run_dir / "sample_summary.csv"
    error_path = run_dir / "errors.log"
    manifest_path = run_dir / "manifest.json"

    source_text = (
        "explicit --symbols"
        if args.symbols
        else str(Path(args.universe_csv).expanduser().resolve())
    )

    manifest: dict[str, Any] = {
        "probe": "universe_quote_watch",
        "started_at_et": started_at.isoformat(),
        "symbol_source": source_text,
        "symbol_count": len(symbols),
        "symbols": list(symbols),
        "fields": args.fields,
        "batch_size": args.batch_size,
        "interval_seconds": args.interval,
        "stop_at_et": stop_at.isoformat() if stop_at else None,
        "max_samples": args.max_samples,
        "timeout_seconds": args.timeout,
        "ecfg_path": str(ecfg_path),
        "acquisition_samples_file": str(acquisition_path),
        "quote_csv_file": str(quote_csv_path),
        "summary_csv_file": str(summary_csv_path),
        "errors_file": str(error_path),
        "completed_at_et": None,
        "samples_attempted": 0,
        "samples_completed": 0,
    }
    write_manifest(manifest_path, manifest)

    print()
    print("Schwab full-universe quote watch")
    print("=" * 79)
    print(f"Symbol source    : {source_text}")
    print(f"Symbols          : {len(symbols)}")
    print(f"Fields           : {args.fields}")
    print(f"Batch size       : {args.batch_size}")
    print(f"Interval         : {args.interval:g} seconds")
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
    print()

    password = getpass.getpass("Encrypted config password: ")

    client = None
    sample_number = 0
    completed_count = 0
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

                sample_number += 1
                sample_started_at = datetime.now(ET)
                started_monotonic = time_module.monotonic()

                try:
                    result = fetch_quotes_batched(
                        client,
                        symbols,
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

                    for item in result.results:
                        quote_writer.writerow(
                            quote_csv_row(
                                sample_number=sample_number,
                                observed_at=sample_completed_at,
                                item=item,
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
                        input_symbol_count=len(symbols),
                        result=result,
                        change_metrics=change_metrics,
                    )
                    row["sample_elapsed_seconds"] = round(
                        elapsed_monotonic,
                        6,
                    )
                    summary_writer.writerow(row)
                    force_flush(summary_csv_file)

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

                if (
                    args.max_samples is not None
                    and sample_number >= args.max_samples
                ):
                    print("Reached maximum sample count.")
                    break

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
        write_manifest(manifest_path, manifest)

        print()
        print("Universe quote watch finished")
        print("=" * 79)
        print(f"Completed         : {completed_at:%Y-%m-%d %H:%M:%S %Z}")
        print(f"Samples attempted : {sample_number}")
        print(f"Samples completed : {completed_count}")
        print(f"Acquisitions      : {acquisition_path}")
        print(f"Quote CSV         : {quote_csv_path}")
        print(f"Summary CSV       : {summary_csv_path}")
        print(f"Errors            : {error_path}")
        print(f"Manifest          : {manifest_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
