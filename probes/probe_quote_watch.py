"""
Watch Schwab multi-symbol quote data over time.

Purpose
-------
This is exploratory diagnostic code, not production code.

The immediate goal is to observe how Schwab quote fields behave across:

    - EXTO overnight trading
    - midnight
    - 01:00 ET, our overnight-volume window start
    - 07:00 ET, normal Schwab pre-market start
    - 09:25 ET, opening Watchlist decision cutoff
    - 09:30 ET, regular market open

The probe:

    - requests multiple symbols in ONE Schwab quotes() call;
    - requests all available quote fields;
    - preserves every raw response in JSONL format;
    - writes selected useful fields to CSV;
    - flushes data to disk after every sample;
    - can stop automatically at a specified Eastern Time;
    - can run a short fixed number of samples for testing.

All human-facing times use America/New_York.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import time as time_module
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mb_tools.schwab_secure import (
    console_auth_callback,
    make_secure_schwab_client,
)


ET = ZoneInfo("America/New_York")

DEFAULT_SYMBOLS = [
    "SPY",
    "QQQ",
    "AAPL",
    "NVDA",
    "AMZN",
    "TLT",
]


CSV_FIELDS = [
    "sample_number",
    "observed_at_et",
    "observed_at_utc",
    "symbol",
    "http_status",

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
    "quote_quoteTime_ms",
    "quote_quoteTime_et",
    "quote_tradeTime_ms",
    "quote_tradeTime_et",
    "quote_closePrice",
    "quote_openPrice",
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Watch Schwab multi-symbol quotes over time."
    )

    parser.add_argument(
        "--symbols",
        nargs="+",
        default=DEFAULT_SYMBOLS,
        help=(
            "Symbols to watch. "
            "Default: SPY QQQ AAPL NVDA AMZN TLT"
        ),
    )

    parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Seconds between samples. Default: 60",
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
        help=(
            "Stop after this many samples. "
            "Useful for short tests."
        ),
    )

    parser.add_argument(
        "--ecfg",
        help="Explicit path to secure_schwabdev.ecfg.",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Schwab request timeout in seconds. Default: 10",
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
            Path(mb_vault).expanduser()
            / "secure_schwabdev.ecfg"
        )

    candidates.append(
        Path.cwd() / "secure_schwabdev.ecfg"
    )

    for path in candidates:
        if path.is_file():
            return path.resolve()

    searched = "\n".join(
        f"  {path}"
        for path in candidates
    )

    raise FileNotFoundError(
        "Could not find secure_schwabdev.ecfg.\n"
        "Paths checked:\n"
        f"{searched}"
    )


def normalize_symbols(
    symbols: list[str],
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    for item in symbols:
        symbol = item.strip().upper()

        if not symbol:
            continue

        if symbol not in seen:
            result.append(symbol)
            seen.add(symbol)

    if not result:
        raise SystemExit(
            "No valid symbols were supplied."
        )

    return result


def parse_et_datetime(
    text: str | None,
) -> datetime | None:
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


def nested(
    value: dict[str, Any],
    *keys: str,
) -> Any:
    current: Any = value

    for key in keys:
        if not isinstance(current, dict):
            return None

        current = current.get(key)

        if current is None:
            return None

    return current


def epoch_ms_to_et_string(
    value: Any,
) -> str | None:
    if not isinstance(value, (int, float)):
        return None

    if value <= 0:
        return None

    try:
        dt = datetime.fromtimestamp(
            value / 1000.0,
            tz=ET,
        )

        return dt.isoformat()

    except (
        OSError,
        OverflowError,
        ValueError,
    ):
        return None


def csv_row(
    *,
    sample_number: int,
    observed_at: datetime,
    symbol: str,
    http_status: int,
    item: dict[str, Any],
) -> dict[str, Any]:

    ext_quote_time = nested(
        item,
        "extended",
        "quoteTime",
    )

    ext_trade_time = nested(
        item,
        "extended",
        "tradeTime",
    )

    quote_quote_time = nested(
        item,
        "quote",
        "quoteTime",
    )

    quote_trade_time = nested(
        item,
        "quote",
        "tradeTime",
    )

    regular_trade_time = nested(
        item,
        "regular",
        "regularMarketTradeTime",
    )

    return {
        "sample_number":
            sample_number,

        "observed_at_et":
            observed_at.isoformat(),

        "observed_at_utc":
            observed_at.astimezone(
                timezone.utc
            ).isoformat(),

        "symbol":
            symbol,

        "http_status":
            http_status,

        "assetMainType":
            item.get("assetMainType"),

        "assetSubType":
            item.get("assetSubType"),

        "realtime":
            item.get("realtime"),

        "extended_bidPrice":
            nested(
                item,
                "extended",
                "bidPrice",
            ),

        "extended_askPrice":
            nested(
                item,
                "extended",
                "askPrice",
            ),

        "extended_mark":
            nested(
                item,
                "extended",
                "mark",
            ),

        "extended_lastPrice":
            nested(
                item,
                "extended",
                "lastPrice",
            ),

        "extended_lastSize":
            nested(
                item,
                "extended",
                "lastSize",
            ),

        "extended_totalVolume":
            nested(
                item,
                "extended",
                "totalVolume",
            ),

        "extended_quoteTime_ms":
            ext_quote_time,

        "extended_quoteTime_et":
            epoch_ms_to_et_string(
                ext_quote_time
            ),

        "extended_tradeTime_ms":
            ext_trade_time,

        "extended_tradeTime_et":
            epoch_ms_to_et_string(
                ext_trade_time
            ),

        "quote_bidPrice":
            nested(
                item,
                "quote",
                "bidPrice",
            ),

        "quote_askPrice":
            nested(
                item,
                "quote",
                "askPrice",
            ),

        "quote_mark":
            nested(
                item,
                "quote",
                "mark",
            ),

        "quote_lastPrice":
            nested(
                item,
                "quote",
                "lastPrice",
            ),

        "quote_lastSize":
            nested(
                item,
                "quote",
                "lastSize",
            ),

        "quote_totalVolume":
            nested(
                item,
                "quote",
                "totalVolume",
            ),

        "quote_quoteTime_ms":
            quote_quote_time,

        "quote_quoteTime_et":
            epoch_ms_to_et_string(
                quote_quote_time
            ),

        "quote_tradeTime_ms":
            quote_trade_time,

        "quote_tradeTime_et":
            epoch_ms_to_et_string(
                quote_trade_time
            ),

        "quote_closePrice":
            nested(
                item,
                "quote",
                "closePrice",
            ),

        "quote_openPrice":
            nested(
                item,
                "quote",
                "openPrice",
            ),

        "quote_postMarketChange":
            nested(
                item,
                "quote",
                "postMarketChange",
            ),

        "quote_postMarketPercentChange":
            nested(
                item,
                "quote",
                "postMarketPercentChange",
            ),

        "regular_regularMarketLastPrice":
            nested(
                item,
                "regular",
                "regularMarketLastPrice",
            ),

        "regular_regularMarketLastSize":
            nested(
                item,
                "regular",
                "regularMarketLastSize",
            ),

        "regular_regularMarketTradeTime_ms":
            regular_trade_time,

        "regular_regularMarketTradeTime_et":
            epoch_ms_to_et_string(
                regular_trade_time
            ),

        "regular_regularMarketNetChange":
            nested(
                item,
                "regular",
                "regularMarketNetChange",
            ),

        "regular_regularMarketPercentChange":
            nested(
                item,
                "regular",
                "regularMarketPercentChange",
            ),

        "fundamental_sharesOutstanding":
            nested(
                item,
                "fundamental",
                "sharesOutstanding",
            ),

        "reference_cusip":
            nested(
                item,
                "reference",
                "cusip",
            ),

        "reference_description":
            nested(
                item,
                "reference",
                "description",
            ),

        "reference_exchange":
            nested(
                item,
                "reference",
                "exchange",
            ),

        "reference_exchangeName":
            nested(
                item,
                "reference",
                "exchangeName",
            ),
    }


def printable_time(
    value: Any,
) -> str:
    text = epoch_ms_to_et_string(value)

    if text is None:
        return "-"

    try:
        dt = datetime.fromisoformat(text)

        return dt.strftime(
            "%m-%d %H:%M:%S"
        )

    except ValueError:
        return text


def print_symbol_summary(
    symbol: str,
    item: dict[str, Any],
) -> None:
    quote_volume = nested(
        item,
        "quote",
        "totalVolume",
    )

    ext_volume = nested(
        item,
        "extended",
        "totalVolume",
    )

    ext_last = nested(
        item,
        "extended",
        "lastPrice",
    )

    ext_trade_time = nested(
        item,
        "extended",
        "tradeTime",
    )

    quote_last = nested(
        item,
        "quote",
        "lastPrice",
    )

    quote_trade_time = nested(
        item,
        "quote",
        "tradeTime",
    )

    print(
        f"  {symbol:<6} "
        f"qVol={str(quote_volume):>12}  "
        f"extVol={str(ext_volume):>10}  "
        f"extLast={str(ext_last):>10}  "
        f"extTrade={printable_time(ext_trade_time):<17}  "
        f"qLast={str(quote_last):>10}  "
        f"qTrade={printable_time(quote_trade_time)}"
    )


def force_flush(file) -> None:
    file.flush()

    try:
        os.fsync(file.fileno())
    except OSError:
        pass


def main() -> int:
    args = parse_args()

    if args.interval <= 0:
        raise SystemExit(
            "--interval must be greater than zero."
        )

    if (
        args.max_samples is not None
        and args.max_samples <= 0
    ):
        raise SystemExit(
            "--max-samples must be greater than zero."
        )

    symbols = normalize_symbols(
        args.symbols
    )

    stop_at = parse_et_datetime(
        args.stop_at
    )

    ecfg_path = resolve_ecfg(
        args.ecfg
    )

    started_at = datetime.now(ET)

    run_stamp = started_at.strftime(
        "%Y-%m-%d-%H-%M-%S"
    )

    run_dir = (
        Path("output")
        / "quote_watch"
        / run_stamp
    )

    run_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    raw_path = (
        run_dir
        / "raw_samples.jsonl"
    )

    csv_path = (
        run_dir
        / "quote_samples.csv"
    )

    error_path = (
        run_dir
        / "errors.log"
    )

    manifest_path = (
        run_dir
        / "manifest.json"
    )

    manifest = {
        "probe": "quote_watch",
        "started_at_et":
            started_at.isoformat(),
        "symbols":
            symbols,
        "interval_seconds":
            args.interval,
        "stop_at_et":
            (
                stop_at.isoformat()
                if stop_at
                else None
            ),
        "max_samples":
            args.max_samples,
        "ecfg_path":
            str(ecfg_path),
        "raw_samples_file":
            str(raw_path),
        "csv_file":
            str(csv_path),
        "errors_file":
            str(error_path),
        "completed_at_et":
            None,
        "samples_attempted":
            0,
        "samples_succeeded":
            0,
    }

    with manifest_path.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            manifest,
            file,
            indent=2,
            sort_keys=True,
        )

    print()
    print("Schwab multi-symbol quote watch")
    print("=" * 79)
    print(
        "Symbols          : "
        + " ".join(symbols)
    )
    print(
        f"Interval         : "
        f"{args.interval:g} seconds"
    )
    print(
        "Started          : "
        f"{started_at:%Y-%m-%d %H:%M:%S %Z}"
    )

    if stop_at:
        print(
            "Automatic stop   : "
            f"{stop_at:%Y-%m-%d %H:%M:%S %Z}"
        )
    else:
        print(
            "Automatic stop   : none"
        )

    if args.max_samples:
        print(
            "Maximum samples  : "
            f"{args.max_samples}"
        )
    else:
        print(
            "Maximum samples  : none"
        )

    print(
        f"Encrypted config : {ecfg_path}"
    )
    print(
        f"Output directory : {run_dir}"
    )
    print()

    password = getpass.getpass(
        "Encrypted config password: "
    )

    client = None
    sample_number = 0
    success_count = 0

    try:
        client = make_secure_schwab_client(
            ecfg_path,
            password,
            timeout=args.timeout,
            call_on_auth=console_auth_callback,
        )

        with (
            raw_path.open(
                "a",
                encoding="utf-8",
            ) as raw_file,
            csv_path.open(
                "a",
                newline="",
                encoding="utf-8",
            ) as csv_file,
            error_path.open(
                "a",
                encoding="utf-8",
            ) as error_file,
        ):
            writer = csv.DictWriter(
                csv_file,
                fieldnames=CSV_FIELDS,
            )

            if csv_file.tell() == 0:
                writer.writeheader()
                force_flush(csv_file)

            next_sample_monotonic = (
                time_module.monotonic()
            )

            while True:
                now = datetime.now(ET)

                if (
                    stop_at is not None
                    and now >= stop_at
                ):
                    print()
                    print(
                        "Reached automatic stop time."
                    )
                    break

                if (
                    args.max_samples is not None
                    and sample_number
                    >= args.max_samples
                ):
                    print()
                    print(
                        "Reached maximum sample count."
                    )
                    break

                sample_number += 1

                requested_at = datetime.now(ET)

                print()
                print(
                    f"Sample {sample_number}  "
                    f"{requested_at:%Y-%m-%d %H:%M:%S %Z}"
                )

                try:
                    response = client.quotes(
                        symbols,
                        fields="all",
                    )

                    received_at = datetime.now(ET)

                    raw_record: dict[str, Any] = {
                        "sample_number":
                            sample_number,
                        "requested_at_et":
                            requested_at.isoformat(),
                        "received_at_et":
                            received_at.isoformat(),
                        "http_status":
                            response.status_code,
                        "request_url":
                            (
                                response.request.url
                                if response.request
                                is not None
                                else None
                            ),
                    }

                    if response.ok:
                        data = response.json()

                        raw_record["response"] = data

                        raw_file.write(
                            json.dumps(
                                raw_record,
                                sort_keys=True,
                            )
                            + "\n"
                        )

                        force_flush(raw_file)

                        for symbol in symbols:
                            item = data.get(
                                symbol,
                                {},
                            )

                            row = csv_row(
                                sample_number=
                                    sample_number,
                                observed_at=
                                    received_at,
                                symbol=
                                    symbol,
                                http_status=
                                    response.status_code,
                                item=
                                    item,
                            )

                            writer.writerow(row)

                            print_symbol_summary(
                                symbol,
                                item,
                            )

                        force_flush(csv_file)

                        success_count += 1

                    else:
                        raw_record["response_text"] = (
                            response.text
                        )

                        raw_file.write(
                            json.dumps(
                                raw_record,
                                sort_keys=True,
                            )
                            + "\n"
                        )

                        force_flush(raw_file)

                        message = (
                            f"{received_at.isoformat()} "
                            f"sample={sample_number} "
                            f"HTTP={response.status_code} "
                            f"{response.text}\n"
                        )

                        error_file.write(message)
                        force_flush(error_file)

                        print(
                            f"  HTTP error "
                            f"{response.status_code}"
                        )

                except Exception as exc:
                    failed_at = datetime.now(ET)

                    message = (
                        f"{failed_at.isoformat()} "
                        f"sample={sample_number} "
                        f"{type(exc).__name__}: "
                        f"{exc}\n"
                    )

                    error_file.write(message)
                    force_flush(error_file)

                    print(
                        "  ERROR: "
                        f"{type(exc).__name__}: "
                        f"{exc}"
                    )

                #
                # Keep the cadence based on a monotonic clock
                # rather than sleeping a fixed interval after
                # each HTTP request.  This minimizes long-term
                # timing drift.
                #

                next_sample_monotonic += (
                    args.interval
                )

                delay = (
                    next_sample_monotonic
                    - time_module.monotonic()
                )

                if delay > 0:
                    time_module.sleep(delay)
                else:
                    #
                    # If a request took longer than the interval,
                    # reset the schedule rather than trying to
                    # "catch up" with rapid requests.
                    #
                    next_sample_monotonic = (
                        time_module.monotonic()
                    )

    except KeyboardInterrupt:
        print()
        print("Stopped by user.")

    finally:
        if client is not None:
            client.close()

        completed_at = datetime.now(ET)

        manifest["completed_at_et"] = (
            completed_at.isoformat()
        )

        manifest["samples_attempted"] = (
            sample_number
        )

        manifest["samples_succeeded"] = (
            success_count
        )

        with manifest_path.open(
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                manifest,
                file,
                indent=2,
                sort_keys=True,
            )

        print()
        print("Quote watch finished")
        print("=" * 79)
        print(
            f"Completed        : "
            f"{completed_at:%Y-%m-%d %H:%M:%S %Z}"
        )
        print(
            f"Samples attempted: "
            f"{sample_number}"
        )
        print(
            f"Samples succeeded: "
            f"{success_count}"
        )
        print(
            f"Raw samples      : "
            f"{raw_path}"
        )
        print(
            f"CSV samples      : "
            f"{csv_path}"
        )
        print(
            f"Errors           : "
            f"{error_path}"
        )
        print(
            f"Manifest         : "
            f"{manifest_path}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
