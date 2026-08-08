"""
Probe Schwab price-history start/end range behavior.

Purpose
-------
Determine whether Schwab honors the intraday portions of startDate and
endDate for five-minute price-history requests.

We request several very different time ranges for the SAME trading day
and compare the number, first timestamp, and last timestamp of the
candles returned.

This is exploratory diagnostic code, not production code.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from mb_tools.schwab_secure import (
    console_auth_callback,
    make_secure_schwab_client,
)


ET = ZoneInfo("America/New_York")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe Schwab intraday price-history range behavior."
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


def et_datetime(
    trade_date: date,
    hour: int,
    minute: int,
) -> datetime:
    return datetime.combine(
        trade_date,
        time(hour=hour, minute=minute),
        tzinfo=ET,
    )


def epoch_ms_to_et(value: int | str) -> datetime:
    return datetime.fromtimestamp(
        int(value) / 1000.0,
        tz=ET,
    )


def candle_dt_et(candle: dict[str, Any]) -> datetime:
    return epoch_ms_to_et(candle["datetime"])


def fmt_time(dt: datetime | None) -> str:
    if dt is None:
        return "-"

    return dt.strftime("%H:%M")


def main() -> int:
    args = parse_args()

    symbol = args.symbol.strip().upper()
    trade_date = parse_trade_date(args.date)
    ecfg_path = resolve_ecfg(args.ecfg)

    #
    # Each tuple contains:
    #
    #   label
    #   start hour
    #   start minute
    #   end hour
    #   end minute
    #   include extended-hours data
    #
    cases = [
        ("A", 0, 30, 10, 0, True),
        ("B", 7, 0, 8, 0, True),
        ("C", 8, 0, 8, 30, True),
        ("D", 9, 20, 9, 30, True),
        ("E", 12, 0, 12, 30, True),
        ("F", 18, 0, 18, 30, True),
        ("G", 8, 0, 8, 30, False),
    ]

    print()
    print("Schwab price-history range probe")
    print("=" * 79)
    print(f"Symbol           : {symbol}")
    print(f"Trading date     : {trade_date}")
    print(f"Frequency        : 5 minutes")
    print(f"Encrypted config : {ecfg_path}")
    print()
    print(
        "This probe makes seven price-history requests for the same "
        "trading date."
    )
    print()

    password = getpass.getpass("Encrypted config password: ")

    client = None
    results: list[dict[str, Any]] = []

    try:
        client = make_secure_schwab_client(
            ecfg_path,
            password,
            timeout=args.timeout,
            call_on_auth=console_auth_callback,
        )

        for (
            label,
            start_hour,
            start_minute,
            end_hour,
            end_minute,
            extended,
        ) in cases:

            request_start = et_datetime(
                trade_date,
                start_hour,
                start_minute,
            )

            request_end = et_datetime(
                trade_date,
                end_hour,
                end_minute,
            )

            print(
                f"Request {label}: "
                f"{request_start:%H:%M}-"
                f"{request_end:%H:%M} ET, "
                f"extended={extended}"
            )

            response = client.price_history(
                symbol,
                frequencyType="minute",
                frequency=5,
                startDate=request_start,
                endDate=request_end,
                needExtendedHoursData=extended,
                needPreviousClose=True,
            )

            if not response.ok:
                print(
                    f"  HTTP {response.status_code}: "
                    f"{response.text}"
                )

                results.append(
                    {
                        "case": label,
                        "requested_start_et": (
                            request_start.isoformat()
                        ),
                        "requested_end_et": (
                            request_end.isoformat()
                        ),
                        "extended": extended,
                        "http_status": response.status_code,
                        "error": response.text,
                    }
                )

                continue

            data = response.json()
            candles = data.get("candles", [])

            if candles:
                first_dt = candle_dt_et(candles[0])
                last_dt = candle_dt_et(candles[-1])
            else:
                first_dt = None
                last_dt = None

            parsed_url = urlparse(response.request.url)
            query = parse_qs(parsed_url.query)

            result = {
                "case": label,
                "requested_start_et": request_start.isoformat(),
                "requested_end_et": request_end.isoformat(),
                "extended": extended,
                "http_status": response.status_code,
                "candle_count": len(candles),
                "first_candle_et": (
                    first_dt.isoformat()
                    if first_dt is not None
                    else None
                ),
                "last_candle_et": (
                    last_dt.isoformat()
                    if last_dt is not None
                    else None
                ),
                "previous_close": data.get("previousClose"),
                "actual_request_url": response.request.url,
                "actual_startDate": (
                    query.get("startDate", [None])[0]
                ),
                "actual_endDate": (
                    query.get("endDate", [None])[0]
                ),
            }

            results.append(result)

            print(
                f"  HTTP={response.status_code}  "
                f"candles={len(candles)}  "
                f"first={fmt_time(first_dt)}  "
                f"last={fmt_time(last_dt)}"
            )

        #
        # Compact comparison table
        #

        print()
        print("Comparison")
        print("=" * 79)

        print(
            f"{'Case':<5}"
            f"{'Requested':<16}"
            f"{'Ext':<7}"
            f"{'Count':>7}  "
            f"{'First':<8}"
            f"{'Last':<8}"
        )

        print(
            f"{'-' * 4:<5}"
            f"{'-' * 15:<16}"
            f"{'-' * 6:<7}"
            f"{'-' * 7:>7}  "
            f"{'-' * 7:<8}"
            f"{'-' * 7:<8}"
        )

        for result in results:
            if result.get("http_status") != 200:
                print(
                    f"{result['case']:<5}"
                    f"{'ERROR':<16}"
                    f"{str(result['extended']):<7}"
                    f"{'-':>7}  "
                    f"{'-':<8}"
                    f"{'-':<8}"
                )
                continue

            requested_start = datetime.fromisoformat(
                result["requested_start_et"]
            )

            requested_end = datetime.fromisoformat(
                result["requested_end_et"]
            )

            requested = (
                f"{requested_start:%H:%M}-"
                f"{requested_end:%H:%M}"
            )

            first = (
                datetime.fromisoformat(
                    result["first_candle_et"]
                )
                if result["first_candle_et"]
                else None
            )

            last = (
                datetime.fromisoformat(
                    result["last_candle_et"]
                )
                if result["last_candle_et"]
                else None
            )

            print(
                f"{result['case']:<5}"
                f"{requested:<16}"
                f"{str(result['extended']):<7}"
                f"{result['candle_count']:>7}  "
                f"{fmt_time(first):<8}"
                f"{fmt_time(last):<8}"
            )

        #
        # Check whether all extended-hours cases returned the same
        # candle-range signature.
        #

        extended_signatures = []

        for result in results:
            if (
                result.get("http_status") == 200
                and result.get("extended") is True
            ):
                extended_signatures.append(
                    (
                        result.get("candle_count"),
                        result.get("first_candle_et"),
                        result.get("last_candle_et"),
                    )
                )

        all_extended_same = (
            len(extended_signatures) > 1
            and len(set(extended_signatures)) == 1
        )

        print()
        print("Interpretation aid")
        print("=" * 79)
        print(
            "All extended-hours cases have identical "
            f"count/first/last: {all_extended_same}"
        )

        #
        # Save a compact evidence file.
        #

        run_stamp = datetime.now(ET).strftime(
            "%Y-%m-%d-%H-%M-%S"
        )

        run_dir = (
            Path("output")
            / "range_probe"
            / f"{run_stamp}-{symbol}-{trade_date}"
        )

        run_dir.mkdir(parents=True, exist_ok=True)

        report_path = run_dir / "range_report.json"

        report = {
            "probe": "price_history_ranges",
            "symbol": symbol,
            "trade_date": str(trade_date),
            "frequency_type": "minute",
            "frequency": 5,
            "all_extended_same_signature": (
                all_extended_same
            ),
            "cases": results,
        }

        with report_path.open(
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                report,
                file,
                indent=2,
                sort_keys=True,
            )

        print()
        print(f"Saved report: {report_path}")

    finally:
        if client is not None:
            client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
