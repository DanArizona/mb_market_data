"""Retrieve and preserve one historical Schwab daily OHLCV candle."""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mb_tools.schwab_secure import (
    console_auth_callback,
    make_secure_schwab_client,
)


ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class DailyOhlcv:
    symbol: str
    session_date: str
    datetime_et: str
    datetime_epoch_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Retrieve one historical daily OHLCV candle from Schwab and "
            "preserve the raw response."
        )
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument(
        "--date",
        required=True,
        type=parse_trade_date,
        help="Trading date in YYYY-MM-DD form.",
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
        default="output/daily_ohlcv",
    )
    return parser.parse_args()


def parse_trade_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}; expected YYYY-MM-DD"
        ) from error


def resolve_ecfg(explicit_path: str | None) -> Path:
    candidates: list[Path] = []

    if explicit_path:
        candidates.append(Path(explicit_path).expanduser())

    environment_path = os.environ.get("MB_SCHWAB_ECFG")
    if environment_path:
        candidates.append(Path(environment_path).expanduser())

    vault = os.environ.get("MB_VAULT")
    if vault:
        candidates.append(Path(vault).expanduser() / "secure_schwabdev.ecfg")

    candidates.append(Path.cwd() / "secure_schwabdev.ecfg")

    for path in candidates:
        if path.is_file():
            return path.resolve()

    searched = "\n".join(f"  {path}" for path in candidates)
    raise FileNotFoundError(
        "Could not find secure_schwabdev.ecfg.\n"
        f"Paths checked:\n{searched}"
    )


def candle_datetime_et(candle: Mapping[str, Any]) -> datetime:
    value = candle.get("datetime")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("daily candle datetime must be numeric epoch milliseconds")
    return datetime.fromtimestamp(value / 1000.0, tz=ET)


def numeric_field(candle: Mapping[str, Any], name: str) -> float:
    value = candle.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"daily candle field {name!r} must be numeric")
    return float(value)


def volume_field(candle: Mapping[str, Any]) -> int:
    value = candle.get("volume")
    if isinstance(value, bool):
        raise ValueError("daily candle volume must be a nonnegative integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float) and value.is_integer():
        result = int(value)
    else:
        raise ValueError("daily candle volume must be a nonnegative integer")
    if result < 0:
        raise ValueError("daily candle volume must be a nonnegative integer")
    return result


def select_daily_ohlcv(
    payload: Mapping[str, Any],
    *,
    symbol: str,
    trade_date: date,
) -> DailyOhlcv:
    candles = payload.get("candles")
    if not isinstance(candles, list):
        raise ValueError("Schwab response does not contain a candle list")

    matching: list[Mapping[str, Any]] = []
    available_dates: list[str] = []

    for candle in candles:
        if not isinstance(candle, Mapping):
            continue
        candle_datetime = candle_datetime_et(candle)
        available_dates.append(candle_datetime.date().isoformat())
        if candle_datetime.date() == trade_date:
            matching.append(candle)

    if not matching:
        available = ", ".join(sorted(set(available_dates))) or "none"
        raise ValueError(
            f"no daily candle returned for {symbol} on {trade_date}; "
            f"available dates: {available}"
        )
    if len(matching) != 1:
        raise ValueError(
            f"Schwab returned {len(matching)} daily candles for "
            f"{symbol} on {trade_date}"
        )

    candle = matching[0]
    candle_datetime = candle_datetime_et(candle)
    timestamp = candle["datetime"]

    return DailyOhlcv(
        symbol=symbol.strip().upper(),
        session_date=trade_date.isoformat(),
        datetime_et=candle_datetime.isoformat(),
        datetime_epoch_ms=int(timestamp),
        open=numeric_field(candle, "open"),
        high=numeric_field(candle, "high"),
        low=numeric_field(candle, "low"),
        close=numeric_field(candle, "close"),
        volume=volume_field(candle),
    )


def write_ohlcv(path: Path, candle: DailyOhlcv) -> None:
    with path.open("x", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(DailyOhlcv.__dataclass_fields__))
        writer.writeheader()
        writer.writerow(asdict(candle))


def request_daily_history(
    client: Any,
    *,
    symbol: str,
    start_at: datetime,
    end_at: datetime,
) -> Any:
    """Request daily candles with a Schwab-compatible period type."""

    return client.price_history(
        symbol,
        periodType="year",
        frequencyType="daily",
        frequency=1,
        startDate=start_at,
        endDate=end_at,
        needExtendedHoursData=False,
        needPreviousClose=True,
    )


def main() -> int:
    args = parse_args()
    symbol = args.symbol.strip().upper()
    if not symbol:
        print("Daily OHLCV probe ERROR: symbol must not be blank", file=sys.stderr)
        return 1

    try:
        ecfg_path = resolve_ecfg(args.ecfg)
    except (OSError, ValueError) as error:
        print(
            f"Daily OHLCV probe ERROR: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    request_start = datetime.combine(
        args.date - timedelta(days=4),
        time.min,
        tzinfo=ET,
    )
    request_end = datetime.combine(
        args.date + timedelta(days=5),
        time.min,
        tzinfo=ET,
    )

    print("Schwab daily OHLCV probe")
    print("=" * 79)
    print(f"Symbol           : {symbol}")
    print(f"Requested date   : {args.date}")
    print(f"Request window   : {request_start.date()} through {request_end.date()}")
    print(f"Encrypted config : {ecfg_path}")
    print()

    password = getpass.getpass("Encrypted config password: ")
    client = None

    try:
        client = make_secure_schwab_client(
            ecfg_path,
            password,
            timeout=args.timeout,
            call_on_auth=console_auth_callback,
        )
        response = request_daily_history(
            client,
            symbol=symbol,
            start_at=request_start,
            end_at=request_end,
        )
        if not response.ok:
            print(
                f"Daily OHLCV probe ERROR: Schwab HTTP "
                f"{response.status_code}: {response.text}",
                file=sys.stderr,
            )
            return 1
        payload = response.json()
    except Exception as error:
        print(
            f"Daily OHLCV probe ERROR: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    finally:
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    run_stamp = datetime.now(ET).strftime("%Y-%m-%d-%H-%M-%S")
    output_dir = (
        Path(args.output_root)
        / f"{run_stamp}-{symbol}-{args.date.isoformat()}"
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    raw_path = output_dir / "price_history_raw.json"
    csv_path = output_dir / "daily_ohlcv.csv"

    with raw_path.open("x", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, sort_keys=True)
        file.write("\n")

    try:
        candle = select_daily_ohlcv(
            payload,
            symbol=symbol,
            trade_date=args.date,
        )
    except ValueError as error:
        print(f"Daily OHLCV probe ERROR: {error}", file=sys.stderr)
        print(f"Raw response      : {raw_path}", file=sys.stderr)
        return 1

    write_ohlcv(csv_path, candle)

    print()
    print("Daily OHLCV result: PASS")
    print("=" * 79)
    print(f"Session date      : {candle.session_date}")
    print(f"Open              : {candle.open}")
    print(f"High              : {candle.high}")
    print(f"Low               : {candle.low}")
    print(f"Close             : {candle.close}")
    print(f"Volume            : {candle.volume:,}")
    print(f"Candle timestamp  : {candle.datetime_et}")
    print(f"Previous close    : {payload.get('previousClose')}")
    print(f"Raw response      : {raw_path}")
    print(f"OHLCV CSV         : {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
