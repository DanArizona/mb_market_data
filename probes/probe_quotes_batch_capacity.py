"""
Probe Schwab multi-symbol quote batch capacity.

Purpose
-------
Determine how many symbols can be requested successfully with one
Schwab /marketdata/v1/quotes HTTP request.

Schwab may return:

    {
        "AAPL": {...},
        "MSFT": {...},
        "errors": {
            "invalidSymbols": [...]
        }
    }

The top-level "errors" key is response metadata, not a ticker symbol.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import time
from pathlib import Path
from typing import Any

from mb_tools.schwab_secure import (
    console_auth_callback,
    make_secure_schwab_client,
)


DEFAULT_CSV = Path(
    "probes/evidence/2026-08-10-watchlist2.csv"
)

DEFAULT_SIZES = [
    10,
    50,
    100,
    250,
    400,
    500,
    750,
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe Schwab multi-symbol quotes batch capacity."
    )

    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_CSV,
        help="CSV containing a Symbol column.",
    )

    parser.add_argument(
        "--sizes",
        type=int,
        nargs="+",
        default=DEFAULT_SIZES,
        help=(
            "Batch sizes to test. "
            "Default: 10 50 100 250 400 500 750"
        ),
    )

    parser.add_argument(
        "--fields",
        default="quote",
        choices=[
            "quote",
            "fundamental",
            "all",
        ],
        help="Schwab quote fields to request. Default: quote",
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

    return parser.parse_args()


def resolve_ecfg(
    explicit_path: str | None,
) -> Path:
    candidates: list[Path] = []

    if explicit_path:
        candidates.append(
            Path(explicit_path).expanduser()
        )

    env_ecfg = os.environ.get(
        "MB_SCHWAB_ECFG"
    )

    if env_ecfg:
        candidates.append(
            Path(env_ecfg).expanduser()
        )

    mb_vault = os.environ.get(
        "MB_VAULT"
    )

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


def read_symbols(
    csv_path: Path,
) -> list[str]:
    with csv_path.open(
        "r",
        newline="",
        encoding="utf-8-sig",
    ) as file:
        rows = list(csv.reader(file))

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
            f"No Symbol header found in {csv_path}"
        )

    symbols: list[str] = []
    seen: set[str] = set()

    for row in rows[header_index + 1 :]:
        if not row:
            continue

        symbol = row[0].strip().upper()

        if not symbol:
            continue

        if symbol in seen:
            continue

        seen.add(symbol)
        symbols.append(symbol)

    return symbols


def quote_symbols_from_payload(
    payload: Any,
) -> set[str]:
    """
    Return actual symbol keys from a Schwab quote response.

    Response metadata such as "errors" is excluded.
    """

    if not isinstance(payload, dict):
        return set()

    return {
        str(key).strip().upper()
        for key in payload
        if str(key).casefold() != "errors"
    }


def invalid_symbols_from_payload(
    payload: Any,
) -> set[str]:
    """
    Return symbols Schwab identifies as invalid.
    """

    if not isinstance(payload, dict):
        return set()

    errors = payload.get("errors")

    if not isinstance(errors, dict):
        return set()

    invalid = errors.get("invalidSymbols")

    if not isinstance(invalid, list):
        return set()

    return {
        str(symbol).strip().upper()
        for symbol in invalid
        if str(symbol).strip()
    }


def main() -> int:
    args = parse_args()

    csv_path = args.csv.resolve()

    if not csv_path.is_file():
        raise FileNotFoundError(csv_path)

    symbols = read_symbols(csv_path)

    sizes = sorted(
        {
            size
            for size in args.sizes
            if size > 0
        }
    )

    if not sizes:
        raise ValueError(
            "At least one positive batch size is required."
        )

    ecfg_path = resolve_ecfg(args.ecfg)

    print()
    print("Schwab quotes batch-capacity probe")
    print("=" * 100)
    print(f"Symbol source     : {csv_path}")
    print(f"Unique symbols    : {len(symbols)}")
    print(f"Requested sizes   : {sizes}")
    print(f"Fields            : {args.fields}")
    print(f"Encrypted config  : {ecfg_path}")
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

    output_root = (
        Path("output")
        / "quotes_batch_capacity"
    )

    output_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    results: list[dict[str, Any]] = []

    print()
    print(
        "Batch   HTTP   Quotes   Invalid   Missing   "
        "URL chars   Wall sec   Resp sec   Result"
    )
    print("-" * 100)

    try:
        for requested_size in sizes:
            actual_size = min(
                requested_size,
                len(symbols),
            )

            batch = symbols[:actual_size]
            expected_symbols = set(batch)

            started = time.perf_counter()

            try:
                response = client.quotes(
                    batch,
                    fields=args.fields,
                )

                wall_seconds = (
                    time.perf_counter()
                    - started
                )

                status_code = response.status_code

                request_url = getattr(
                    response.request,
                    "url",
                    "",
                ) or ""

                url_length = len(request_url)

                response_seconds = (
                    response.elapsed.total_seconds()
                    if response.elapsed is not None
                    else None
                )

                try:
                    payload = response.json()
                except Exception:
                    payload = None

                quote_symbols = (
                    quote_symbols_from_payload(
                        payload
                    )
                )

                invalid_symbols = (
                    invalid_symbols_from_payload(
                        payload
                    )
                )

                accounted_for = (
                    quote_symbols
                    | invalid_symbols
                )

                missing_symbols = (
                    expected_symbols
                    - accounted_for
                )

                extra_symbols = (
                    quote_symbols
                    - expected_symbols
                )

                if not response.ok:
                    result = "HTTP_FAIL"

                elif (
                    missing_symbols
                    or extra_symbols
                ):
                    result = "PARTIAL"

                elif invalid_symbols:
                    result = "PASS_INVALID"

                else:
                    result = "PASS"

                print(
                    f"{actual_size:>5}   "
                    f"{status_code:>4}   "
                    f"{len(quote_symbols):>6}   "
                    f"{len(invalid_symbols):>7}   "
                    f"{len(missing_symbols):>7}   "
                    f"{url_length:>9}   "
                    f"{wall_seconds:>8.3f}   "
                    f"{response_seconds:>8.3f}   "
                    f"{result}"
                )

                raw_path = (
                    output_root
                    / f"quotes_{actual_size}.json"
                )

                with raw_path.open(
                    "w",
                    encoding="utf-8",
                ) as file:
                    if payload is not None:
                        json.dump(
                            payload,
                            file,
                            indent=2,
                            sort_keys=True,
                        )
                    else:
                        file.write(response.text)

                results.append(
                    {
                        "requested":
                            actual_size,

                        "http_status":
                            status_code,

                        "quotes_returned":
                            len(quote_symbols),

                        "invalid_count":
                            len(invalid_symbols),

                        "missing_count":
                            len(missing_symbols),

                        "extra_count":
                            len(extra_symbols),

                        "url_length":
                            url_length,

                        "wall_seconds":
                            wall_seconds,

                        "response_seconds":
                            response_seconds,

                        "result":
                            result,

                        "invalid_symbols":
                            " ".join(
                                sorted(
                                    invalid_symbols
                                )
                            ),

                        "missing_symbols":
                            " ".join(
                                sorted(
                                    missing_symbols
                                )
                            ),

                        "extra_symbols":
                            " ".join(
                                sorted(
                                    extra_symbols
                                )
                            ),
                    }
                )

            except Exception as exc:
                wall_seconds = (
                    time.perf_counter()
                    - started
                )

                print(
                    f"{actual_size:>5}   "
                    f"{'ERR':>4}   "
                    f"{'':>6}   "
                    f"{'':>7}   "
                    f"{'':>7}   "
                    f"{'':>9}   "
                    f"{wall_seconds:>8.3f}   "
                    f"{'':>8}   "
                    f"{type(exc).__name__}: {exc}"
                )

                results.append(
                    {
                        "requested":
                            actual_size,
                        "http_status":
                            "",
                        "quotes_returned":
                            "",
                        "invalid_count":
                            "",
                        "missing_count":
                            "",
                        "extra_count":
                            "",
                        "url_length":
                            "",
                        "wall_seconds":
                            wall_seconds,
                        "response_seconds":
                            "",
                        "result":
                            (
                                "ERROR: "
                                f"{type(exc).__name__}: "
                                f"{exc}"
                            ),
                        "invalid_symbols":
                            "",
                        "missing_symbols":
                            "",
                        "extra_symbols":
                            "",
                    }
                )

    finally:
        try:
            client.close()
        except Exception:
            pass

    report_path = (
        output_root
        / "report.csv"
    )

    fieldnames = [
        "requested",
        "http_status",
        "quotes_returned",
        "invalid_count",
        "missing_count",
        "extra_count",
        "url_length",
        "wall_seconds",
        "response_seconds",
        "result",
        "invalid_symbols",
        "missing_symbols",
        "extra_symbols",
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
        writer.writerows(results)

    print()
    print("=" * 100)
    print(f"Report            : {report_path}")
    print(f"Raw responses     : {output_root}")
    print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
