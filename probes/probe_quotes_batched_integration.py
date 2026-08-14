"""
Integration probe for production batched Schwab quote acquisition.

Uses the real preserved ~759-symbol ThinkOrSwim universe and the
production mb_market_data.schwab_quotes.fetch_quotes_batched() function.
"""

from __future__ import annotations

import getpass
import os
import time
import argparse
from pathlib import Path

from mb_market_data.schwab_quotes import (
    QuoteStatus,
    fetch_quotes_batched,
)
from mb_market_data.tos_watchlist import read_tos_watchlist
from mb_tools.schwab_secure import (
    console_auth_callback,
    make_secure_schwab_client,
)


WATCHLIST_CSV = Path(
    "probes/evidence/2026-08-10-watchlist2.csv"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Integration probe for production batched "
            "Schwab quote acquisition."
        )
    )

    parser.add_argument(
        "--fields",
        choices=[
            "quote",
            "fundamental",
            "all",
        ],
        default="all",
        help="Schwab quote fields to request. Default: all",
    )

    return parser.parse_args()


def resolve_ecfg() -> Path:
    env_ecfg = os.environ.get("MB_SCHWAB_ECFG")

    if env_ecfg:
        path = Path(env_ecfg).expanduser()

        if path.is_file():
            return path.resolve()

    mb_vault = os.environ.get("MB_VAULT")

    if mb_vault:
        path = (
            Path(mb_vault).expanduser()
            / "secure_schwabdev.ecfg"
        )

        if path.is_file():
            return path.resolve()

    path = Path("secure_schwabdev.ecfg")

    if path.is_file():
        return path.resolve()

    raise FileNotFoundError(
        "Could not locate secure_schwabdev.ecfg."
    )


def main() -> int:
    watchlist = read_tos_watchlist(
        WATCHLIST_CSV
    )

    args = parse_args()

    symbols = [
        row.symbol
        for row in watchlist.rows
    ]

    ecfg_path = resolve_ecfg()

    print()
    print("Production batched-quotes integration probe")
    print("=" * 72)
    print(f"Watchlist       : {WATCHLIST_CSV.resolve()}")
    print(f"Symbols         : {len(symbols)}")
    print(f"Encrypted config: {ecfg_path}")
    print(f"Batch size      : 400")
    print(f"Fields          : {args.fields}")
    print()

    password = getpass.getpass(
        "Encrypted config password: "
    )

    client = make_secure_schwab_client(
        ecfg_path,
        password,
        timeout=20,
        call_on_auth=console_auth_callback,
    )

    started = time.perf_counter()

    try:
        result = fetch_quotes_batched(
            client,
            symbols,
            fields=args.fields,
            batch_size=400,
        )
    finally:
        try:
            client.close()
        except Exception:
            pass

    elapsed = time.perf_counter() - started

    counts = result.status_counts()

    print()
    print("=" * 72)
    print(f"Input symbols       : {len(symbols)}")
    print(f"Results             : {len(result.results)}")
    print(f"HTTP requests       : {result.request_count}")
    print(f"Quote               : {counts[QuoteStatus.QUOTE]}")
    print(f"Invalid             : {counts[QuoteStatus.INVALID]}")
    print(f"Missing             : {counts[QuoteStatus.MISSING]}")
    print(f"Request errors      : {counts[QuoteStatus.REQUEST_ERROR]}")
    print(f"Unexpected symbols  : {len(result.unexpected_symbols)}")
    print(f"Elapsed seconds     : {elapsed:.3f}")
    print()

    invalid = [
        item.symbol
        for item in result.results
        if item.status == QuoteStatus.INVALID
    ]

    missing = [
        item.symbol
        for item in result.results
        if item.status == QuoteStatus.MISSING
    ]

    request_errors = [
        item.symbol
        for item in result.results
        if item.status == QuoteStatus.REQUEST_ERROR
    ]

    if invalid:
        print(
            "Invalid symbols      : "
            + " ".join(invalid)
        )

    if missing:
        print(
            "Missing symbols      : "
            + " ".join(missing)
        )

    if request_errors:
        print(
            "Request-error symbols: "
            + " ".join(request_errors)
        )

    print()

    if (
        len(result.results) == len(symbols)
        and result.request_count == 2
        and counts[QuoteStatus.MISSING] == 0
        and counts[QuoteStatus.REQUEST_ERROR] == 0
        and not result.unexpected_symbols
    ):
        print(
            "Result: PASS - every requested symbol was "
            "explicitly accounted for."
        )
        return 0

    print(
        "Result: FAIL - acquisition had unaccounted-for "
        "or request-level failures."
    )

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
