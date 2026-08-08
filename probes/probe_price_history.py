"""
Probe Schwab five-minute price-history data for one symbol.

Purpose
-------
This is an exploratory probe, not production code.

It requests an explicitly bounded interval of five-minute price history
with extended-hours data enabled, preserves the raw Schwab JSON response,
writes the returned candles to CSV, and prints a simple Eastern-Time
inventory.

The initial use case is determining whether Schwab price history can
support the mb_market_data overnight-volume definitions:

    OV_DECISION : 01:00 <= ET < 09:25
    PREMARKET   : 07:00 <= ET < 09:25
    OV_FINAL    : 01:00 <= ET < 09:30
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mb_tools.schwab_secure import (
    console_auth_callback,
    make_secure_schwab_client,
)


ET = ZoneInfo("America/New_York")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe Schwab five-minute extended-hours price history."
    )

    parser.add_argument(
        "--symbol",
        default="SPY",
        help="Symbol to probe. Default: SPY",
    )

    parser.add_argument(
        "--date",
        required=True,
        help="Trading date to inspect, YYYY-MM-DD.",
    )

    parser.add_argument(
        "--ecfg",
        help=(
            "Path to secure_schwabdev.ecfg. "
            "If omitted, MB_SCHWAB_ECFG, MB_VAULT, and the current "
            "directory are checked."
        ),
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


def parse_trade_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise SystemExit(
            f"Invalid --date {text!r}; expected YYYY-MM-DD."
        ) from exc


def et_datetime(trade_date: date, hour: int, minute: int) -> datetime:
    return datetime.combine(
        trade_date,
        time(hour=hour, minute=minute),
        tzinfo=ET,
    )


def candle_datetime_et(candle: dict[str, Any]) -> datetime:
    timestamp_ms = candle["datetime"]

    return datetime.fromtimestamp(
        timestamp_ms / 1000.0,
        tz=ET,
    )


def in_window(
    dt: datetime,
    trade_date: date,
    start_hour: int,
    start_minute: int,
    end_hour: int,
    end_minute: int,
) -> bool:
    start = et_datetime(trade_date, start_hour, start_minute)
    end = et_datetime(trade_date, end_hour, end_minute)

    return start <= dt < end


def volume_for_window(
    candles: list[dict[str, Any]],
    trade_date: date,
    start_hour: int,
    start_minute: int,
    end_hour: int,
    end_minute: int,
) -> tuple[int, int]:
    selected = []

    for candle in candles:
        dt = candle_datetime_et(candle)

        if in_window(
            dt,
            trade_date,
            start_hour,
            start_minute,
            end_hour,
            end_minute,
        ):
            selected.append(candle)

    volume = sum(int(candle.get("volume", 0)) for candle in selected)

    return len(selected), volume


def write_candles_csv(
    path: Path,
    candles: list[dict[str, Any]],
) -> None:
    fieldnames = [
        "datetime_et",
        "datetime_epoch_ms",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]

    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()

        for candle in candles:
            dt = candle_datetime_et(candle)

            writer.writerow(
                {
                    "datetime_et": dt.isoformat(),
                    "datetime_epoch_ms": candle.get("datetime"),
                    "open": candle.get("open"),
                    "high": candle.get("high"),
                    "low": candle.get("low"),
                    "close": candle.get("close"),
                    "volume": candle.get("volume"),
                }
            )


def print_candle_inventory(
    candles: list[dict[str, Any]],
    trade_date: date,
) -> None:
    print()
    print("Returned candle inventory")
    print("=" * 79)

    if not candles:
        print("No candles returned.")
        return

    for candle in candles:
        dt = candle_datetime_et(candle)

        print(
            f"{dt:%Y-%m-%d %H:%M:%S %Z}  "
            f"O={candle.get('open')}  "
            f"H={candle.get('high')}  "
            f"L={candle.get('low')}  "
            f"C={candle.get('close')}  "
            f"V={candle.get('volume')}"
        )

    print()
    print("Summary")
    print("=" * 79)

    first_dt = candle_datetime_et(candles[0])
    last_dt = candle_datetime_et(candles[-1])

    print(f"Returned candles : {len(candles)}")
    print(f"First timestamp  : {first_dt:%Y-%m-%d %H:%M:%S %Z}")
    print(f"Last timestamp   : {last_dt:%Y-%m-%d %H:%M:%S %Z}")

    windows = [
        ("OV_DECISION", 1, 0, 9, 25),
        ("PREMARKET", 7, 0, 9, 25),
        ("OV_FINAL", 1, 0, 9, 30),
    ]

    print()

    for name, sh, sm, eh, em in windows:
        count, volume = volume_for_window(
            candles,
            trade_date,
            sh,
            sm,
            eh,
            em,
        )

        print(
            f"{name:<12} "
            f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d} ET   "
            f"candles={count:3d}   "
            f"volume={volume:,}"
        )


def main() -> int:
    args = parse_args()

    symbol = args.symbol.strip().upper()
    trade_date = parse_trade_date(args.date)
    ecfg_path = resolve_ecfg(args.ecfg)

    # Ask Schwab for a little more than the eventual overnight window.
    # Seeing the surrounding candles is useful during this investigation.
    request_start = et_datetime(trade_date, 0, 30)
    request_end = et_datetime(trade_date, 10, 0)

    print()
    print("Schwab price-history probe")
    print("=" * 79)
    print(f"Symbol        : {symbol}")
    print(f"Trading date  : {trade_date}")
    print(
        "Request range : "
        f"{request_start:%Y-%m-%d %H:%M %Z} "
        f"through {request_end:%Y-%m-%d %H:%M %Z}"
    )
    print("Frequency     : 5 minutes")
    print("Extended hrs  : YES")
    print("Previous close: YES")
    print(f"Encrypted cfg : {ecfg_path}")
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

        print()
        print("Requesting Schwab price history...")

        response = client.price_history(
            symbol,
            frequencyType="minute",
            frequency=5,
            startDate=request_start,
            endDate=request_end,
            needExtendedHoursData=True,
            needPreviousClose=True,
        )

        print(f"HTTP status   : {response.status_code}")

        if not response.ok:
            print()
            print("Schwab request failed.")
            print(response.text)
            return 1

        data = response.json()

    finally:
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    run_stamp = datetime.now(ET).strftime("%Y-%m-%d-%H-%M-%S")
    run_dir = (
        Path("output")
        / "overnight_probe"
        / f"{run_stamp}-{symbol}-{trade_date}"
    )

    run_dir.mkdir(parents=True, exist_ok=True)

    raw_path = run_dir / "price_history_raw.json"
    csv_path = run_dir / "candles.csv"

    with raw_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, sort_keys=True)

    candles = data.get("candles", [])

    write_candles_csv(csv_path, candles)

    print()
    print(f"Raw JSON      : {raw_path}")
    print(f"Candle CSV    : {csv_path}")

    print()
    print("Top-level Schwab response fields")
    print("=" * 79)

    for key in sorted(data):
        if key != "candles":
            print(f"{key}: {data[key]!r}")

    print_candle_inventory(candles, trade_date)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
