"""
Validate derivation of OV_FINAL from OV_DECISION.

Hypothesis
----------
    OV_FINAL
        = OV_DECISION
        + Schwab 09:25 ET five-minute candle volume

where:

    OV_DECISION = volume from 01:00 <= ET < 09:25
    OV_FINAL    = volume from 01:00 <= ET < 09:30

The ToS evidence CSV contains independently calculated OV_DECISION
and OV_FINAL values.  This probe asks Schwab price_history() for
5-minute candles and compares the 09:25 candle volume against the
difference between those two ToS values.

All displayed market times use America/New_York.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import re
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mb_tools.schwab_secure import (
    console_auth_callback,
    make_secure_schwab_client,
)


ET = ZoneInfo("America/New_York")


DEFAULT_CSV = Path(
    "probes/evidence/"
    "2026-08-10-watchlist-export-OV_FINAL-OV_DECISION.csv"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate OV_FINAL = OV_DECISION "
            "+ Schwab 09:25 five-minute volume."
        )
    )

    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_CSV,
        help=(
            "ToS Watchlist export containing Symbol, "
            "OV_DECISION, and OV_FINAL."
        ),
    )

    parser.add_argument(
        "--date",
        help=(
            "Trading date YYYY-MM-DD. "
            "If omitted, infer it from the CSV filename."
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
        help="Schwab REST timeout in seconds. Default: 10",
    )

    return parser.parse_args()


def resolve_ecfg(
    explicit_path: str | None,
) -> Path:
    candidates: list[Path] = []

    if explicit_path:
        candidates.append(
            Path(explicit_path).expanduser()
        )

    env_ecfg = os.environ.get("MB_SCHWAB_ECFG")

    if env_ecfg:
        candidates.append(
            Path(env_ecfg).expanduser()
        )

    mb_vault = os.environ.get("MB_VAULT")

    if mb_vault:
        candidates.append(
            Path(mb_vault).expanduser()
            / "secure_schwabdev.ecfg"
        )

    candidates.append(
        Path.cwd()
        / "secure_schwabdev.ecfg"
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


def infer_trade_date(
    csv_path: Path,
    explicit_date: str | None,
) -> datetime:
    if explicit_date:
        text = explicit_date
    else:
        match = re.search(
            r"\d{4}-\d{2}-\d{2}",
            csv_path.name,
        )

        if not match:
            raise ValueError(
                "Could not infer trading date from "
                f"{csv_path.name!r}. "
                "Supply --date YYYY-MM-DD."
            )

        text = match.group(0)

    try:
        return datetime.strptime(
            text,
            "%Y-%m-%d",
        ).replace(
            tzinfo=ET
        )

    except ValueError as exc:
        raise ValueError(
            f"Invalid trading date: {text!r}"
        ) from exc


def read_tos_watchlist(
    path: Path,
) -> list[dict[str, str]]:
    """
    Read a ToS Watchlist CSV.

    Rather than assuming exactly three preamble lines,
    locate the row whose first field is 'Symbol'.
    """

    with path.open(
        "r",
        newline="",
        encoding="utf-8-sig",
    ) as file:
        rows = list(
            csv.reader(file)
        )

    header_index = None

    for index, row in enumerate(rows):
        if (
            row
            and row[0].strip() == "Symbol"
        ):
            header_index = index
            break

    if header_index is None:
        raise ValueError(
            f"No Symbol header found in {path}"
        )

    header = [
        item.strip()
        for item in rows[header_index]
    ]

    required = {
        "Symbol",
        "OV_DECISION",
        "OV_FINAL",
    }

    missing = required - set(header)

    if missing:
        raise ValueError(
            "CSV is missing required column(s): "
            + ", ".join(sorted(missing))
        )

    result: list[dict[str, str]] = []

    for values in rows[
        header_index + 1:
    ]:
        if not values:
            continue

        if len(values) < len(header):
            values = values + (
                [""] * (
                    len(header)
                    - len(values)
                )
            )

        record = dict(
            zip(
                header,
                values,
            )
        )

        symbol = (
            record.get(
                "Symbol",
                ""
            )
            .strip()
            .upper()
        )

        if not symbol:
            continue

        record["Symbol"] = symbol
        result.append(record)

    return result


def parse_integral_value(
    text: str,
) -> int | None:
    value = text.strip()

    if not value:
        return None

    if value.lower() in {
        "loading",
        "nan",
        "n/a",
        "na",
    }:
        return None

    if (
        "subscription limit"
        in value.lower()
    ):
        return None

    try:
        number = Decimal(
            value.replace(",", "")
        )

    except InvalidOperation:
        return None

    integral = number.to_integral_value()

    if number != integral:
        raise ValueError(
            "Expected integral volume value, "
            f"got {text!r}"
        )

    return int(integral)


def candle_datetime_et(
    candle: dict[str, Any],
) -> datetime:
    epoch_ms = candle["datetime"]

    return datetime.fromtimestamp(
        epoch_ms / 1000.0,
        tz=ET,
    )


def find_0925_candle(
    candles: list[dict[str, Any]],
    trade_date: datetime,
) -> tuple[
    datetime,
    dict[str, Any],
] | None:

    wanted_date = trade_date.date()
    wanted_time = time(
        9,
        25,
    )

    for candle in candles:
        candle_dt = candle_datetime_et(
            candle
        )

        if (
            candle_dt.date()
            == wanted_date
            and candle_dt.time().replace(
                second=0,
                microsecond=0,
            )
            == wanted_time
        ):
            return (
                candle_dt,
                candle,
            )

    return None


def main() -> int:
    args = parse_args()

    csv_path = args.csv.resolve()

    if not csv_path.is_file():
        raise FileNotFoundError(
            csv_path
        )

    trade_date = infer_trade_date(
        csv_path,
        args.date,
    )

    records = read_tos_watchlist(
        csv_path
    )

    usable: list[
        tuple[str, int, int]
    ] = []

    skipped: list[str] = []

    for record in records:
        symbol = record["Symbol"]

        ov_decision = parse_integral_value(
            record["OV_DECISION"]
        )

        ov_final = parse_integral_value(
            record["OV_FINAL"]
        )

        if (
            ov_decision is None
            or ov_final is None
        ):
            skipped.append(symbol)
            continue

        usable.append(
            (
                symbol,
                ov_decision,
                ov_final,
            )
        )

    ecfg_path = resolve_ecfg(
        args.ecfg
    )

    started_at = datetime.now(ET)

    run_stamp = started_at.strftime(
        "%Y-%m-%d-%H-%M-%S"
    )

    output_dir = (
        Path("output")
        / "ov_final_derivation"
        / run_stamp
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    report_path = (
        output_dir
        / "report.csv"
    )

    print()
    print(
        "OV_FINAL derivation probe"
    )
    print("=" * 79)
    print(
        f"Evidence CSV     : {csv_path}"
    )
    print(
        "Trading date     : "
        f"{trade_date.date()}"
    )
    print(
        f"Symbols in CSV   : {len(records)}"
    )
    print(
        f"Usable symbols   : {len(usable)}"
    )
    print(
        f"Skipped symbols  : {len(skipped)}"
    )
    print(
        f"Encrypted config : {ecfg_path}"
    )
    print(
        f"Output directory : {output_dir}"
    )
    print()

    if skipped:
        print(
            "Skipped because OV values "
            "were not both numeric:"
        )
        print(
            "  "
            + " ".join(skipped)
        )
        print()

    password = getpass.getpass(
        "Encrypted config password: "
    )

    client = make_secure_schwab_client(
        ecfg_path,
        password,
        timeout=args.timeout,
        call_on_auth=console_auth_callback,
    )

    start_dt = trade_date.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    end_dt = trade_date.replace(
        hour=23,
        minute=59,
        second=59,
        microsecond=999999,
    )

    report_rows: list[
        dict[str, Any]
    ] = []

    pass_count = 0
    fail_count = 0
    error_count = 0

    try:
        print(
            "Symbol   OV_DECISION   "
            "09:25 Vol   Derived FINAL   "
            "ToS OV_FINAL   Difference   Result"
        )
        print("-" * 79)

        for (
            symbol,
            ov_decision,
            ov_final,
        ) in usable:

            try:
                response = client.price_history(
                    symbol,
                    frequencyType="minute",
                    frequency=5,
                    startDate=start_dt,
                    endDate=end_dt,
                    needExtendedHoursData=True,
                    needPreviousClose=True,
                )

                raw_path = (
                    output_dir
                    / f"{symbol}_price_history.json"
                )

                if response.ok:
                    data = response.json()

                    with raw_path.open(
                        "w",
                        encoding="utf-8",
                    ) as file:
                        json.dump(
                            data,
                            file,
                            indent=2,
                            sort_keys=True,
                        )

                else:
                    with raw_path.open(
                        "w",
                        encoding="utf-8",
                    ) as file:
                        file.write(
                            response.text
                        )

                    raise RuntimeError(
                        "HTTP "
                        f"{response.status_code}"
                    )

                candles = data.get(
                    "candles",
                    []
                )

                match = find_0925_candle(
                    candles,
                    trade_date,
                )

                if match is None:
                    raise RuntimeError(
                        "No 09:25 ET "
                        "five-minute candle found."
                    )

                candle_dt, candle = match

                candle_volume_raw = (
                    candle.get("volume")
                )

                if not isinstance(
                    candle_volume_raw,
                    (int, float),
                ):
                    raise RuntimeError(
                        "09:25 candle has "
                        "no numeric volume."
                    )

                candle_volume = int(
                    candle_volume_raw
                )

                derived_final = (
                    ov_decision
                    + candle_volume
                )

                difference = (
                    derived_final
                    - ov_final
                )

                if difference == 0:
                    result = "PASS"
                    pass_count += 1
                else:
                    result = "FAIL"
                    fail_count += 1

                print(
                    f"{symbol:<7}"
                    f"{ov_decision:>12,}   "
                    f"{candle_volume:>9,}   "
                    f"{derived_final:>13,}   "
                    f"{ov_final:>12,}   "
                    f"{difference:>10,}   "
                    f"{result}"
                )

                report_rows.append(
                    {
                        "symbol":
                            symbol,

                        "trade_date":
                            str(
                                trade_date.date()
                            ),

                        "ov_decision":
                            ov_decision,

                        "candle_time_et":
                            candle_dt.isoformat(),

                        "candle_volume":
                            candle_volume,

                        "derived_ov_final":
                            derived_final,

                        "tos_ov_final":
                            ov_final,

                        "difference":
                            difference,

                        "result":
                            result,
                    }
                )

            except Exception as exc:
                error_count += 1

                print(
                    f"{symbol:<7}"
                    f"{ov_decision:>12,}   "
                    f"{'ERROR':>9}   "
                    f"{'':>13}   "
                    f"{ov_final:>12,}   "
                    f"{'':>10}   "
                    f"{type(exc).__name__}: "
                    f"{exc}"
                )

                report_rows.append(
                    {
                        "symbol":
                            symbol,

                        "trade_date":
                            str(
                                trade_date.date()
                            ),

                        "ov_decision":
                            ov_decision,

                        "candle_time_et":
                            "",

                        "candle_volume":
                            "",

                        "derived_ov_final":
                            "",

                        "tos_ov_final":
                            ov_final,

                        "difference":
                            "",

                        "result":
                            (
                                "ERROR: "
                                f"{type(exc).__name__}: "
                                f"{exc}"
                            ),
                    }
                )

    finally:
        try:
            client.close()
        except Exception:
            pass

    fieldnames = [
        "symbol",
        "trade_date",
        "ov_decision",
        "candle_time_et",
        "candle_volume",
        "derived_ov_final",
        "tos_ov_final",
        "difference",
        "result",
    ]

    with report_path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
        )

        writer.writeheader()
        writer.writerows(
            report_rows
        )

    print()
    print("=" * 79)
    print(
        f"PASS   : {pass_count}"
    )
    print(
        f"FAIL   : {fail_count}"
    )
    print(
        f"ERROR  : {error_count}"
    )
    print(
        f"Report : {report_path}"
    )
    print()

    if (
        fail_count == 0
        and error_count == 0
        and pass_count > 0
    ):
        print(
            "Result: hypothesis validated "
            "for every tested symbol."
        )
        return 0

    print(
        "Result: one or more symbols "
        "did not validate."
    )

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
