"""
Probe the complete Schwab quote response for one symbol.

Purpose
-------
This is exploratory diagnostic code, not production code.

The immediate goal is to discover exactly what Schwab exposes in a
single-symbol quote response, especially fields that may help with:

    - overnight / EXTO activity
    - total volume
    - trade time
    - bid / ask / mark
    - regular-market values
    - extended-hours values
    - previous close
    - market capitalization
    - fundamental / share information
    - security identity

The complete raw JSON response is preserved beneath output/ so that
nothing is lost merely because we do not yet understand a field.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
from datetime import datetime
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
        description="Probe the complete Schwab quote response."
    )

    parser.add_argument(
        "--symbol",
        default="SPY",
        help="Symbol to probe. Default: SPY",
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


def possible_timestamp_et(
    key: str,
    value: Any,
) -> str | None:
    """
    If a field looks like a date/time field containing a Unix timestamp,
    return a human-readable Eastern Time interpretation.

    This is only an interpretation aid.  The original value is always
    printed and preserved unchanged.
    """

    if not isinstance(value, (int, float)):
        return None

    key_lower = key.lower()

    if "time" not in key_lower and "date" not in key_lower:
        return None

    try:
        # Millisecond epoch values are currently around 1.7e12.
        if 1_000_000_000_000 <= value < 10_000_000_000_000:
            dt = datetime.fromtimestamp(
                value / 1000.0,
                tz=ET,
            )

            return dt.strftime("%Y-%m-%d %H:%M:%S.%f %Z")

        # Some APIs also use epoch seconds.
        if 1_000_000_000 <= value < 10_000_000_000:
            dt = datetime.fromtimestamp(
                value,
                tz=ET,
            )

            return dt.strftime("%Y-%m-%d %H:%M:%S %Z")

    except (OSError, OverflowError, ValueError):
        return None

    return None


def print_value(
    path: str,
    key: str,
    value: Any,
) -> None:
    interpreted = possible_timestamp_et(key, value)

    if interpreted is None:
        print(f"{path:<60} = {value!r}")
    else:
        print(
            f"{path:<60} = {value!r}  "
            f"[ET: {interpreted}]"
        )


def print_tree(
    value: Any,
    path: str = "",
) -> None:
    """
    Recursively print every field in a JSON-compatible object.

    Dictionary paths are displayed using dots.  Array indexes use
    square brackets.
    """

    if isinstance(value, dict):
        if not value:
            print(f"{path:<60} = {{}}")
            return

        for key in sorted(value):
            child_path = f"{path}.{key}" if path else str(key)

            child_value = value[key]

            if isinstance(child_value, (dict, list)):
                print_tree(
                    child_value,
                    child_path,
                )
            else:
                print_value(
                    child_path,
                    str(key),
                    child_value,
                )

        return

    if isinstance(value, list):
        if not value:
            print(f"{path:<60} = []")
            return

        for index, item in enumerate(value):
            child_path = f"{path}[{index}]"

            if isinstance(item, (dict, list)):
                print_tree(
                    item,
                    child_path,
                )
            else:
                print_value(
                    child_path,
                    str(index),
                    item,
                )

        return

    print_value(
        path or "<root>",
        path or "<root>",
        value,
    )


def collect_field_paths(
    value: Any,
    path: str = "",
) -> list[str]:
    """
    Return the paths of all scalar fields in the JSON response.
    """

    paths: list[str] = []

    if isinstance(value, dict):
        for key, child_value in value.items():
            child_path = f"{path}.{key}" if path else str(key)

            paths.extend(
                collect_field_paths(
                    child_value,
                    child_path,
                )
            )

    elif isinstance(value, list):
        for index, child_value in enumerate(value):
            child_path = f"{path}[{index}]"

            paths.extend(
                collect_field_paths(
                    child_value,
                    child_path,
                )
            )

    else:
        paths.append(path)

    return paths


def main() -> int:
    args = parse_args()

    symbol = args.symbol.strip().upper()
    ecfg_path = resolve_ecfg(args.ecfg)

    print()
    print("Schwab quote probe")
    print("=" * 79)
    print(f"Symbol           : {symbol}")
    print("Fields requested : all")
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

        requested_at = datetime.now(ET)

        print()
        print(
            "Requesting complete Schwab quote at "
            f"{requested_at:%Y-%m-%d %H:%M:%S %Z} ..."
        )

        response = client.quote(
            symbol,
            fields="all",
        )

        received_at = datetime.now(ET)

        print(f"HTTP status      : {response.status_code}")
        print(
            "Response received : "
            f"{received_at:%Y-%m-%d %H:%M:%S %Z}"
        )

        if response.request is not None:
            print(f"Request URL      : {response.request.url}")

        if not response.ok:
            print()
            print("Schwab quote request failed.")
            print(response.text)
            return 1

        data = response.json()

        #
        # Preserve the evidence before interpreting it.
        #

        run_stamp = received_at.strftime(
            "%Y-%m-%d-%H-%M-%S"
        )

        run_dir = (
            Path("output")
            / "quote_probe"
            / f"{run_stamp}-{symbol}"
        )

        run_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        raw_path = run_dir / "quote_raw.json"
        fields_path = run_dir / "quote_field_paths.txt"
        report_path = run_dir / "quote_report.json"

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

        field_paths = sorted(
            collect_field_paths(data)
        )

        with fields_path.open(
            "w",
            encoding="utf-8",
        ) as file:
            for field_path in field_paths:
                file.write(f"{field_path}\n")

        report = {
            "probe": "quote",
            "symbol": symbol,
            "fields_requested": "all",
            "requested_at_et": requested_at.isoformat(),
            "received_at_et": received_at.isoformat(),
            "http_status": response.status_code,
            "request_url": (
                response.request.url
                if response.request is not None
                else None
            ),
            "scalar_field_count": len(field_paths),
            "raw_json_file": str(raw_path),
            "field_paths_file": str(fields_path),
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
        print("Saved evidence")
        print("=" * 79)
        print(f"Raw JSON    : {raw_path}")
        print(f"Field paths : {fields_path}")
        print(f"Report      : {report_path}")

        print()
        print(
            f"Scalar fields returned: {len(field_paths)}"
        )

        print()
        print("Complete quote field inventory")
        print("=" * 79)

        print_tree(data)

    finally:
        if client is not None:
            client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
