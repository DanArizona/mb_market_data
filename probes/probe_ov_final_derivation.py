"""
Integration validation for OV_FINAL derivation.

This probe intentionally uses the production mb_market_data modules:

    tos_watchlist.py
        -> read OV_DECISION from a ThinkOrSwim export

    schwab_candles.py
        -> fetch the 09:25 ET five-minute Schwab candle

    overnight_volume.py
        -> derive OV_FINAL

The derived result is compared with the independently calculated
OV_FINAL value preserved in the ThinkOrSwim evidence CSV.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import os
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from mb_market_data.overnight_volume import derive_ov_final
from mb_market_data.schwab_candles import fetch_0925_candle
from mb_market_data.tos_watchlist import read_tos_watchlist
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
            "Validate production OV_FINAL derivation "
            "against preserved ThinkOrSwim evidence."
        )
    )

    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_CSV,
        help="ToS CSV containing OV_DECISION and OV_FINAL.",
    )

    parser.add_argument(
        "--date",
        help=(
            "Trading date YYYY-MM-DD. "
            "If omitted, infer from the CSV filename."
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
        help="Schwab REST timeout in seconds.",
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


def resolve_trade_date(
    csv_path: Path,
    explicit_date: str | None,
) -> date:
    if explicit_date:
        text = explicit_date
    else:
        match = re.search(
            r"\d{4}-\d{2}-\d{2}",
            csv_path.name,
        )

        if match is None:
            raise ValueError(
                "Could not infer trading date from "
                f"{csv_path.name!r}; use --date."
            )

        text = match.group(0)

    return datetime.strptime(
        text,
        "%Y-%m-%d",
    ).date()


def parse_integral_volume(
    raw_value: str,
) -> int | None:
    """
    Parse a nonnegative integral volume from a ToS field.

    Returns None for blank/non-numeric/unavailable values.
    """

    text = raw_value.strip()

    if not text:
        return None

    normalized = text.casefold()

    if normalized in {
        "loading",
        "nan",
        "<empty>",
    }:
        return None

    if "subscription limit" in normalized:
        return None

    try:
        value = Decimal(
            text.replace(",", "")
        )
    except InvalidOperation:
        return None

    if not value.is_finite():
        return None

    integral = value.to_integral_value()

    if value != integral:
        return None

    result = int(integral)

    if result < 0:
        return None

    return result


def main() -> int:
    args = parse_args()

    csv_path = args.csv.resolve()

    if not csv_path.is_file():
        raise FileNotFoundError(csv_path)

    trade_date = resolve_trade_date(
        csv_path,
        args.date,
    )

    watchlist = read_tos_watchlist(
        csv_path
    )

    if "OV_FINAL" not in watchlist.headers:
        raise ValueError(
            "Evidence CSV does not contain OV_FINAL."
        )

    usable_rows = []

    for row in watchlist.rows:
        if not row.usable_ov_decision:
            continue

        tos_ov_final = parse_integral_volume(
            row.fields.get(
                "OV_FINAL",
                "",
            )
        )

        if tos_ov_final is None:
            continue

        usable_rows.append(
            (
                row,
                tos_ov_final,
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

    report_path = output_dir / "report.csv"

    print()
    print("OV_FINAL production integration probe")
    print("=" * 79)
    print(f"Evidence CSV     : {csv_path}")
    print(f"Trading date     : {trade_date}")
    print(f"Symbols in CSV   : {len(watchlist.rows)}")
    print(f"Usable symbols   : {len(usable_rows)}")
    print(f"Encrypted config : {ecfg_path}")
    print(f"Output directory : {output_dir}")
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

    report_rows = []

    pass_count = 0
    fail_count = 0
    error_count = 0

    print(
        "Symbol   OV_DECISION   "
        "09:25 Vol   Derived FINAL   "
        "ToS OV_FINAL   Difference   Result"
    )
    print("-" * 79)

    try:
        for row, tos_ov_final in usable_rows:
            symbol = row.symbol
            ov_decision = row.ov_decision

            assert ov_decision is not None

            try:
                candle = fetch_0925_candle(
                    client,
                    symbol=symbol,
                    trade_date=trade_date,
                )

                derived_ov_final = derive_ov_final(
                    ov_decision,
                    candle.volume,
                )

                difference = (
                    derived_ov_final
                    - tos_ov_final
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
                    f"{candle.volume:>9,}   "
                    f"{derived_ov_final:>13,}   "
                    f"{tos_ov_final:>12,}   "
                    f"{difference:>10,}   "
                    f"{result}"
                )

                report_rows.append(
                    {
                        "symbol": symbol,
                        "trade_date": trade_date,
                        "ov_decision": ov_decision,
                        "candle_time_et":
                            candle.start_et.isoformat(),
                        "candle_volume":
                            candle.volume,
                        "derived_ov_final":
                            derived_ov_final,
                        "tos_ov_final":
                            tos_ov_final,
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
                    f"{tos_ov_final:>12,}   "
                    f"{'':>10}   "
                    f"{type(exc).__name__}: {exc}"
                )

                report_rows.append(
                    {
                        "symbol": symbol,
                        "trade_date": trade_date,
                        "ov_decision": ov_decision,
                        "candle_time_et": "",
                        "candle_volume": "",
                        "derived_ov_final": "",
                        "tos_ov_final": tos_ov_final,
                        "difference": "",
                        "result": (
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
        writer.writerows(report_rows)

    print()
    print("=" * 79)
    print(f"PASS   : {pass_count}")
    print(f"FAIL   : {fail_count}")
    print(f"ERROR  : {error_count}")
    print(f"Report : {report_path}")
    print()

    if (
        pass_count > 0
        and fail_count == 0
        and error_count == 0
    ):
        print(
            "Result: production modules validated "
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
