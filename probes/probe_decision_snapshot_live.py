"""
Live integration probe for decision-time market-data snapshots.

This probe combines:

    same-day ThinkOrSwim OV_DECISION Watchlist export
        +
    live batched Schwab fields="all" quotes
        |
        v
    immutable DecisionSnapshotBatch

The probe is intended to validate the complete market-open acquisition
path and its timing provenance.

IMPORTANT TIMING MODEL

Schwab authentication is intentionally completed BEFORE the critical
decision path begins. This mirrors the intended production controller,
which should authenticate earlier in the trading day and keep the
authenticated client alive.

The critical path measured by this probe is therefore:

    read/accept ToS Watchlist
        ->
    acquire Schwab quote batches
        ->
    assemble decision snapshots

By default, the trade date must equal today's date in Eastern Time.
This prevents an old ToS OV_DECISION file from accidentally being
combined with current Schwab quotes and described as a live decision
snapshot.

A non-today file may be used only with --allow-non-today. Such a run
tests plumbing only and is NOT valid decision-time market evidence.

Internal timestamps are UTC. Human-facing output is Eastern Time.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from mb_market_data.decision_batch import (
    DecisionSnapshotBatch,
    build_decision_snapshot_batch,
)
from mb_market_data.schwab_quotes import (
    DEFAULT_QUOTE_BATCH_SIZE,
    QuoteBatchResult,
    QuoteStatus,
    fetch_quotes_batched,
)
from mb_market_data.tos_watchlist import (
    OVDecisionStatus,
    read_tos_watchlist,
)
from mb_tools.schwab_secure import (
    console_auth_callback,
    make_secure_schwab_client,
)


UTC = timezone.utc
ET = ZoneInfo("America/New_York")

DATE_PATTERN = re.compile(
    r"(?<!\d)"
    r"(\d{4}-\d{2}-\d{2})"
    r"(?!\d)"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a live immutable decision snapshot "
            "from a ToS Watchlist export and batched "
            "Schwab quotes."
        )
    )

    parser.add_argument(
        "--watchlist",
        required=True,
        type=Path,
        help=(
            "Path to the ThinkOrSwim Watchlist CSV "
            "containing OV_DECISION."
        ),
    )

    parser.add_argument(
        "--trade-date",
        type=date.fromisoformat,
        default=None,
        help=(
            "Trade date YYYY-MM-DD. If omitted, the "
            "first YYYY-MM-DD in the Watchlist filename "
            "is used."
        ),
    )

    parser.add_argument(
        "--fields",
        choices=[
            "quote",
            "fundamental",
            "all",
        ],
        default="all",
        help=(
            "Schwab quote fields to request. "
            "Default: all"
        ),
    )

    parser.add_argument(
        "--allow-non-today",
        action="store_true",
        help=(
            "Allow a trade date other than today's "
            "Eastern Time date. Intended only for "
            "structural/plumbing tests."
        ),
    )

    return parser.parse_args()


def extract_filename_date(
    path: Path,
) -> date | None:
    """
    Extract the first YYYY-MM-DD found in a filename.
    """

    match = DATE_PATTERN.search(
        path.name
    )

    if match is None:
        return None

    try:
        return date.fromisoformat(
            match.group(1)
        )
    except ValueError:
        return None


def resolve_trade_date(
    *,
    watchlist_path: Path,
    explicit_trade_date: date | None,
) -> date:
    """
    Determine the intended trade date.

    If both an explicit date and filename date exist,
    they must agree.
    """

    filename_date = extract_filename_date(
        watchlist_path
    )

    if explicit_trade_date is not None:
        if (
            filename_date is not None
            and filename_date != explicit_trade_date
        ):
            raise ValueError(
                "Explicit --trade-date "
                f"{explicit_trade_date} does not match "
                "the date embedded in the filename "
                f"({filename_date})."
            )

        return explicit_trade_date

    if filename_date is not None:
        return filename_date

    raise ValueError(
        "Could not determine trade date from "
        f"filename {watchlist_path.name!r}. "
        "Supply --trade-date YYYY-MM-DD."
    )


def resolve_ecfg() -> Path:
    """
    Locate the encrypted Schwab configuration.

    Search order:

        MB_SCHWAB_ECFG
        MB_VAULT\\secure_schwabdev.ecfg
        .\\secure_schwabdev.ecfg
    """

    env_ecfg = os.environ.get(
        "MB_SCHWAB_ECFG"
    )

    if env_ecfg:
        path = Path(
            env_ecfg
        ).expanduser()

        if path.is_file():
            return path.resolve()

    mb_vault = os.environ.get(
        "MB_VAULT"
    )

    if mb_vault:
        path = (
            Path(
                mb_vault
            ).expanduser()
            / "secure_schwabdev.ecfg"
        )

        if path.is_file():
            return path.resolve()

    path = Path(
        "secure_schwabdev.ecfg"
    )

    if path.is_file():
        return path.resolve()

    raise FileNotFoundError(
        "Could not locate "
        "secure_schwabdev.ecfg."
    )


def format_et(
    value: datetime | None,
) -> str:
    """
    Format a timestamp in Eastern Time.
    """

    if value is None:
        return "NONE"

    return (
        value
        .astimezone(ET)
        .strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )[:-3]
        + " ET"
    )


def print_status_counts(
    *,
    title: str,
    counts: Counter,
    statuses,
) -> None:
    print(title)
    print("-" * 78)

    for status in statuses:
        print(
            f"{status.value:<24}: "
            f"{counts[status]}"
        )

    print()


def coverage_count(
    snapshots,
    attribute: str,
) -> int:
    return sum(
        1
        for snapshot in snapshots
        if getattr(
            snapshot,
            attribute,
        )
        is not None
    )


def coverage_text(
    count: int,
    total: int,
) -> str:
    if total == 0:
        return (
            f"{count:>4} / {total:<4} "
            "(n/a)"
        )

    percent = (
        count
        / total
        * 100.0
    )

    return (
        f"{count:>4} / {total:<4} "
        f"({percent:6.2f}%)"
    )


def print_quote_coverage(
    batch: DecisionSnapshotBatch,
) -> None:
    quote_snapshots = [
        snapshot
        for snapshot in batch.snapshots
        if snapshot.has_schwab_quote
    ]

    total = len(
        quote_snapshots
    )

    print(
        "Schwab field coverage "
        "(successful quote results)"
    )
    print("-" * 78)

    fields = [
        (
            "sharesOutstanding",
            "shares_outstanding",
        ),
        (
            "bidPrice",
            "bid_price",
        ),
        (
            "askPrice",
            "ask_price",
        ),
        (
            "lastPrice",
            "last_price",
        ),
        (
            "mark",
            "mark",
        ),
        (
            "closePrice",
            "close_price",
        ),
        (
            "regularMarketLastPrice",
            "regular_market_last_price",
        ),
        (
            "totalVolume",
            "total_volume",
        ),
        (
            "quoteTime",
            "quote_time_utc",
        ),
        (
            "tradeTime",
            "trade_time_utc",
        ),
    ]

    for label, attribute in fields:
        count = coverage_count(
            quote_snapshots,
            attribute,
        )

        print(
            f"{label:<28}: "
            f"{coverage_text(count, total)}"
        )

    bid_ask_count = sum(
        1
        for snapshot in quote_snapshots
        if (
            snapshot.bid_price
            is not None
            and snapshot.ask_price
            is not None
        )
    )

    print(
        f"{'bid + ask pair':<28}: "
        f"{coverage_text(bid_ask_count, total)}"
    )

    print()


def print_batch_timing(
    quote_batch: QuoteBatchResult,
) -> None:
    print(
        "Schwab batch acquisition timing"
    )
    print("-" * 78)

    print(
        "Batch   Symbols   "
        "Request started              "
        "Response received            "
        "Duration"
    )

    print("-" * 78)

    batch_numbers = sorted(
        {
            result.batch_number
            for result
            in quote_batch.results
        }
    )

    for batch_number in batch_numbers:
        results = [
            result
            for result
            in quote_batch.results
            if result.batch_number
            == batch_number
        ]

        first = results[0]

        started = (
            first.request_started_at_utc
        )

        received = (
            first.response_received_at_utc
        )

        if received is None:
            duration_text = "n/a"
        else:
            duration = (
                received
                - started
            ).total_seconds()

            duration_text = (
                f"{duration:.3f} s"
            )

        started_text = (
            started
            .astimezone(ET)
            .strftime(
                "%H:%M:%S.%f"
            )[:-3]
        )

        if received is None:
            received_text = "NO RESPONSE"
        else:
            received_text = (
                received
                .astimezone(ET)
                .strftime(
                    "%H:%M:%S.%f"
                )[:-3]
            )

        print(
            f"{batch_number:>5}   "
            f"{len(results):>7}   "
            f"{started_text:<27}"
            f"{received_text:<27}"
            f"{duration_text}"
        )

    print()


def print_source_unready(
    batch: DecisionSnapshotBatch,
) -> None:
    unready = (
        batch.source_unready_snapshots
    )

    if not unready:
        print(
            "Source-unready symbols: NONE"
        )
        print()
        return

    print(
        "Source-unready symbols"
    )
    print("-" * 78)

    print(
        "Symbol       "
        "OV_DECISION status          "
        "Schwab status"
    )

    print("-" * 78)

    for snapshot in unready:
        print(
            f"{snapshot.symbol:<12}"
            f"{snapshot.ov_decision_status.value:<28}"
            f"{snapshot.quote_status.value}"
        )

    print()


def main() -> int:
    args = parse_args()

    watchlist_path = (
        args.watchlist
        .expanduser()
    )

    if not watchlist_path.is_file():
        raise FileNotFoundError(
            f"Watchlist file not found: "
            f"{watchlist_path}"
        )

    watchlist_path = (
        watchlist_path.resolve()
    )

    trade_date = resolve_trade_date(
        watchlist_path=watchlist_path,
        explicit_trade_date=(
            args.trade_date
        ),
    )

    today_et = (
        datetime.now(ET).date()
    )

    if (
        trade_date != today_et
        and not args.allow_non_today
    ):
        raise RuntimeError(
            "Refusing to build a live decision "
            f"snapshot for trade date {trade_date}. "
            f"Today's Eastern Time date is "
            f"{today_et}. "
            "Use --allow-non-today only for an "
            "intentional structural/plumbing test."
        )

    non_today_mode = (
        trade_date != today_et
    )

    print()
    print(
        "Live decision-snapshot integration probe"
    )
    print("=" * 78)

    if non_today_mode:
        print(
            "*** NON-TODAY TEST MODE ***"
        )
        print(
            "The ToS data date does not match "
            "the current Schwab acquisition date."
        )
        print(
            "This run must NOT be treated as "
            "decision-time market evidence."
        )
        print()

    print(
        f"Trade date      : {trade_date}"
    )
    print(
        f"Watchlist       : {watchlist_path}"
    )
    print(
        f"Fields          : {args.fields}"
    )
    print(
        f"Batch size      : "
        f"{DEFAULT_QUOTE_BATCH_SIZE}"
    )
    print()

    #
    # Authenticate Schwab BEFORE the critical path.
    #

    ecfg_path = resolve_ecfg()

    print(
        "Schwab setup"
    )
    print("-" * 78)

    print(
        f"Encrypted config: {ecfg_path}"
    )

    password = getpass.getpass(
        "Encrypted config password: "
    )

    auth_started = (
        time.perf_counter()
    )

    client = make_secure_schwab_client(
        ecfg_path,
        password,
        timeout=20,
        call_on_auth=console_auth_callback,
    )

    auth_elapsed = (
        time.perf_counter()
        - auth_started
    )

    print(
        f"Authentication/setup elapsed: "
        f"{auth_elapsed:.3f} s"
    )

    print()

    try:
        #
        # ==============================================================
        # CRITICAL DECISION PATH STARTS HERE.
        # ==============================================================
        #

        critical_path_started_at_utc = (
            datetime.now(UTC)
        )

        critical_path_started_perf = (
            time.perf_counter()
        )

        #
        # Read and accept the ToS decision data.
        #

        tos_read_started_at_utc = (
            datetime.now(UTC)
        )

        tos_read_started_perf = (
            time.perf_counter()
        )

        watchlist = read_tos_watchlist(
            watchlist_path
        )

        tos_observed_at_utc = (
            datetime.now(UTC)
        )

        tos_read_elapsed = (
            time.perf_counter()
            - tos_read_started_perf
        )

        file_mtime_utc = (
            datetime.fromtimestamp(
                watchlist_path
                .stat()
                .st_mtime,
                tz=UTC,
            )
        )

        file_age_seconds = (
            tos_observed_at_utc
            - file_mtime_utc
        ).total_seconds()

        symbols = [
            row.symbol
            for row in watchlist.rows
        ]

        #
        # Immediately acquire Schwab data.
        #

        schwab_acquisition_started_perf = (
            time.perf_counter()
        )

        quote_batch = (
            fetch_quotes_batched(
                client,
                symbols,
                fields=args.fields,
                batch_size=(
                    DEFAULT_QUOTE_BATCH_SIZE
                ),
            )
        )

        schwab_acquisition_elapsed = (
            time.perf_counter()
            - schwab_acquisition_started_perf
        )

        #
        # Assemble the immutable snapshot batch.
        #

        assembly_started_perf = (
            time.perf_counter()
        )

        decision_batch = (
            build_decision_snapshot_batch(
                trade_date=trade_date,
                watchlist=watchlist,
                quote_batch=quote_batch,
                tos_observed_at_utc=(
                    tos_observed_at_utc
                ),
            )
        )

        assembly_elapsed = (
            time.perf_counter()
            - assembly_started_perf
        )

        critical_path_elapsed = (
            time.perf_counter()
            - critical_path_started_perf
        )

    finally:
        try:
            client.close()
        except Exception:
            pass

    #
    # Report.
    #

    print()
    print("=" * 78)
    print(
        "Decision snapshot summary"
    )
    print("=" * 78)

    print(
        f"ToS rows             : "
        f"{len(watchlist.rows)}"
    )

    print(
        f"Schwab results       : "
        f"{len(quote_batch.results)}"
    )

    print(
        f"Decision snapshots   : "
        f"{len(decision_batch.snapshots)}"
    )

    print(
        f"Source ready         : "
        f"{len(decision_batch.source_ready_snapshots)}"
    )

    print(
        f"Source unready       : "
        f"{len(decision_batch.source_unready_snapshots)}"
    )

    print(
        f"Schwab HTTP requests : "
        f"{quote_batch.request_count}"
    )

    print(
        f"Unexpected symbols   : "
        f"{len(quote_batch.unexpected_symbols)}"
    )

    print(
        "Quote results not in "
        f"Watchlist: "
        f"{len(decision_batch.quote_results_not_in_watchlist)}"
    )

    print()

    #
    # ToS acquisition provenance.
    #

    print(
        "ThinkOrSwim acquisition"
    )
    print("-" * 78)

    print(
        f"File modified        : "
        f"{format_et(file_mtime_utc)}"
    )

    print(
        f"ToS read started     : "
        f"{format_et(tos_read_started_at_utc)}"
    )

    print(
        f"Controller accepted  : "
        f"{format_et(tos_observed_at_utc)}"
    )

    print(
        f"ToS read/parse time  : "
        f"{tos_read_elapsed:.3f} s"
    )

    print(
        f"File age at acceptance: "
        f"{file_age_seconds:.3f} s"
    )

    print()

    #
    # Status counts.
    #

    ov_counts = (
        decision_batch
        .ov_status_counts()
    )

    print_status_counts(
        title="OV_DECISION status counts",
        counts=ov_counts,
        statuses=OVDecisionStatus,
    )

    quote_counts = (
        decision_batch
        .quote_status_counts()
    )

    print_status_counts(
        title="Schwab status counts",
        counts=quote_counts,
        statuses=QuoteStatus,
    )

    #
    # Schwab field coverage.
    #

    print_quote_coverage(
        decision_batch
    )

    #
    # Individual Schwab batch timing.
    #

    print_batch_timing(
        quote_batch
    )

    #
    # Critical-path timing.
    #

    print(
        "Decision-path timing"
    )
    print("-" * 78)

    print(
        f"Critical path start  : "
        f"{format_et(critical_path_started_at_utc)}"
    )

    print(
        f"ToS accepted         : "
        f"{format_et(tos_observed_at_utc)}"
    )

    first_request = None
    final_response = None

    if quote_batch.results:
        first_request = min(
            result.request_started_at_utc
            for result
            in quote_batch.results
        )

        response_times = [
            result.response_received_at_utc
            for result
            in quote_batch.results
            if (
                result.response_received_at_utc
                is not None
            )
        ]

        print(
            f"First Schwab request : "
            f"{format_et(first_request)}"
        )

        if response_times:
            final_response = max(
                response_times
            )

            print(
                f"Final Schwab response: "
                f"{format_et(final_response)}"
            )

    print()

    print(
        f"ToS read/parse       : "
        f"{tos_read_elapsed:.3f} s"
    )

    print(
        f"Schwab acquisition   : "
        f"{schwab_acquisition_elapsed:.3f} s"
    )

    print(
        f"Snapshot assembly    : "
        f"{assembly_elapsed:.3f} s"
    )

    print(
        f"Total critical path  : "
        f"{critical_path_elapsed:.3f} s"
    )

    if (
        first_request is not None
        and final_response is not None
    ):
        tos_to_first_request = (
            first_request
            - tos_observed_at_utc
        ).total_seconds()

        tos_to_schwab_complete = (
            final_response
            - tos_observed_at_utc
        ).total_seconds()

        schwab_request_window = (
            final_response
            - first_request
        ).total_seconds()

        print()

        print(
            f"ToS accepted -> first Schwab request: "
            f"{tos_to_first_request:.3f} s"
        )

        print(
            f"First request -> final response      : "
            f"{schwab_request_window:.3f} s"
        )

        print(
            f"ToS accepted -> Schwab complete      : "
            f"{tos_to_schwab_complete:.3f} s"
        )

    print()

    #
    # Source-unready detail.
    #

    print_source_unready(
        decision_batch
    )

    #
    # Integration success means every requested symbol was explicitly
    # represented. INVALID symbols are legitimate explicit results and
    # therefore do not fail the integration probe.
    #

    integration_ok = (
        len(decision_batch.snapshots)
        == len(watchlist.rows)
        and quote_counts[
            QuoteStatus.MISSING
        ]
        == 0
        and quote_counts[
            QuoteStatus.REQUEST_ERROR
        ]
        == 0
        and not quote_batch.unexpected_symbols
        and not (
            decision_batch
            .quote_results_not_in_watchlist
        )
    )

    print("=" * 78)

    if integration_ok:
        print(
            "Integration result: PASS"
        )

        print(
            "Every ToS symbol was explicitly "
            "accounted for in the immutable "
            "decision snapshot batch."
        )

        print(
            "Decision readiness: "
            f"{len(decision_batch.source_ready_snapshots)}"
            " / "
            f"{len(decision_batch.snapshots)}"
            " symbols have both usable "
            "OV_DECISION and a Schwab quote."
        )

        if non_today_mode:
            print()
            print(
                "Reminder: this was NON-TODAY "
                "TEST MODE and is not valid "
                "decision-time market evidence."
            )

        return 0

    print(
        "Integration result: FAIL"
    )

    print(
        "One or more requested symbols were "
        "not explicitly accounted for, or a "
        "Schwab request-level failure occurred."
    )

    return 1


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
