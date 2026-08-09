"""
Probe Schwab LEVELONE_EQUITIES streaming data.

Purpose
-------
This is exploratory diagnostic code, not production code.

The primary question is whether Schwab exposes useful live information
during the EXTO overnight session (especially 01:00-07:00 ET), even
though REST price_history() does not return those historical bars.

The probe observes multiple symbols using TWO data paths through ONE
authenticated Schwab client:

    1. LEVELONE_EQUITIES streaming
    2. periodic REST quotes(fields="all")

This lets us compare live streaming fields against REST quote fields
without running two independent authenticated Schwab processes.

All human-facing times use America/New_York.

Important
---------
LEVELONE_EQUITIES messages are incremental.  A later message may contain
only one changed field.  Therefore this probe:

    - preserves every raw streaming message;
    - preserves every individual change;
    - maintains a merged current state for each symbol;
    - writes merged state snapshots to CSV;
    - periodically captures full REST quote responses;
    - flushes output frequently so an interrupted overnight run still
      retains the observations already collected.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import queue
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import schwabdev

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


#
# Schwab LEVELONE_EQUITIES field numbers.
#
# We deliberately request every currently documented field rather than
# only the fields that look useful today.  Raw responses are preserved
# so that later investigation can reinterpret them.
#

FIELD_NAMES = {
    "0": "Symbol",
    "1": "Bid Price",
    "2": "Ask Price",
    "3": "Last Price",
    "4": "Bid Size",
    "5": "Ask Size",
    "6": "Ask ID",
    "7": "Bid ID",
    "8": "Total Volume",
    "9": "Last Size",
    "10": "High Price",
    "11": "Low Price",
    "12": "Close Price",
    "13": "Exchange ID",
    "14": "Marginable",
    "15": "Description",
    "16": "Last ID",
    "17": "Open Price",
    "18": "Net Change",
    "19": "52 Week High",
    "20": "52 Week Low",
    "21": "PE Ratio",
    "22": "Annual Dividend Amount",
    "23": "Dividend Yield",
    "24": "NAV",
    "25": "Exchange Name",
    "26": "Dividend Date",
    "27": "Regular Market Quote",
    "28": "Regular Market Trade",
    "29": "Regular Market Last Price",
    "30": "Regular Market Last Size",
    "31": "Regular Market Net Change",
    "32": "Security Status",
    "33": "Mark Price",
    "34": "Quote Time in Long",
    "35": "Trade Time in Long",
    "36": "Regular Market Trade Time in Long",
    "37": "Bid Time",
    "38": "Ask Time",
    "39": "Ask MIC ID",
    "40": "Bid MIC ID",
    "41": "Last MIC ID",
    "42": "Net Percent Change",
    "43": "Regular Market Percent Change",
    "44": "Mark Price Net Change",
    "45": "Mark Price Percent Change",
    "46": "Hard to Borrow Quantity",
    "47": "Hard To Borrow Rate",
    "48": "Hard to Borrow",
    "49": "Shortable",
    "50": "Post-Market Net Change",
    "51": "Post-Market Percent Change",
}

STREAM_FIELDS = list(FIELD_NAMES)


STREAM_CSV_FIELDS = [
    "update_number",
    "received_at_et",
    "received_at_utc",
    "server_timestamp_ms",
    "server_timestamp_et",
    "command",
    "symbol",
    "changed_fields",

    "delayed",
    "assetMainType",
    "assetSubType",
    "cusip",

    "bidPrice",
    "askPrice",
    "lastPrice",
    "bidSize",
    "askSize",
    "totalVolume",
    "lastSize",

    "highPrice",
    "lowPrice",
    "closePrice",
    "openPrice",

    "regularMarketQuote",
    "regularMarketTrade",
    "regularMarketLastPrice",
    "regularMarketLastSize",
    "regularMarketNetChange",

    "securityStatus",
    "markPrice",

    "quoteTime_ms",
    "quoteTime_et",
    "tradeTime_ms",
    "tradeTime_et",
    "regularMarketTradeTime_ms",
    "regularMarketTradeTime_et",
    "bidTime_ms",
    "bidTime_et",
    "askTime_ms",
    "askTime_et",

    "askMICId",
    "bidMICId",
    "lastMICId",

    "netPercentChange",
    "regularMarketPercentChange",
    "markPriceNetChange",
    "markPricePercentChange",
    "postMarketNetChange",
    "postMarketPercentChange",
]


REST_CSV_FIELDS = [
    "sample_number",
    "observed_at_et",
    "observed_at_utc",
    "symbol",
    "http_status",

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

    "regular_lastPrice",
    "regular_lastSize",
    "regular_tradeTime_ms",
    "regular_tradeTime_et",

    "sharesOutstanding",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Probe Schwab LEVELONE_EQUITIES streaming data "
            "and periodic REST quotes."
        )
    )

    parser.add_argument(
        "--symbols",
        nargs="+",
        default=DEFAULT_SYMBOLS,
        help=(
            "Symbols to observe. "
            "Default: SPY QQQ AAPL NVDA AMZN TLT"
        ),
    )

    parser.add_argument(
        "--stop-at",
        help=(
            "Stop automatically at this Eastern Time. "
            "Format: YYYY-MM-DDTHH:MM or YYYY-MM-DDTHH:MM:SS"
        ),
    )

    parser.add_argument(
        "--duration",
        type=float,
        help=(
            "Stop after this many seconds. "
            "Useful for short mechanical tests."
        ),
    )

    parser.add_argument(
        "--max-updates",
        type=int,
        help=(
            "Stop after this many LEVELONE_EQUITIES content updates."
        ),
    )

    parser.add_argument(
        "--rest-interval",
        type=float,
        default=60.0,
        help=(
            "Seconds between REST quote snapshots. "
            "Use 0 to disable REST snapshots. Default: 60"
        ),
    )

    parser.add_argument(
        "--report-interval",
        type=float,
        default=60.0,
        help=(
            "Seconds between console status summaries. "
            "Default: 60"
        ),
    )

    parser.add_argument(
        "--ping-interval",
        type=int,
        default=20,
        help="WebSocket ping interval in seconds. Default: 20",
    )

    parser.add_argument(
        "--ecfg",
        help="Explicit path to secure_schwabdev.ecfg.",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="REST request timeout in seconds. Default: 10",
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


def normalize_symbols(
    values: list[str],
) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()

    for item in values:
        symbol = item.strip().upper()

        if not symbol:
            continue

        if symbol not in seen:
            symbols.append(symbol)
            seen.add(symbol)

    if not symbols:
        raise SystemExit(
            "No valid symbols supplied."
        )

    return symbols


def parse_et_datetime(
    text: str | None,
) -> datetime | None:
    if text is None:
        return None

    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SystemExit(
            f"Invalid datetime {text!r}; "
            "expected YYYY-MM-DDTHH:MM"
        ) from exc

    if dt.tzinfo is None:
        return dt.replace(
            tzinfo=ET
        )

    return dt.astimezone(ET)


def nested(
    value: dict[str, Any],
    *keys: str,
) -> Any:
    current: Any = value

    for key in keys:
        if not isinstance(
            current,
            dict,
        ):
            return None

        current = current.get(key)

        if current is None:
            return None

    return current


def epoch_ms_to_et(
    value: Any,
) -> datetime | None:
    if not isinstance(
        value,
        (int, float),
    ):
        return None

    if value <= 0:
        return None

    #
    # This guard prevents small integers from being
    # misinterpreted as epoch milliseconds.
    #
    if value < 1_000_000_000_000:
        return None

    try:
        return datetime.fromtimestamp(
            value / 1000.0,
            tz=ET,
        )

    except (
        OSError,
        OverflowError,
        ValueError,
    ):
        return None


def epoch_ms_to_et_string(
    value: Any,
) -> str | None:
    dt = epoch_ms_to_et(value)

    if dt is None:
        return None

    return dt.isoformat()


def short_time(
    value: Any,
) -> str:
    dt = epoch_ms_to_et(value)

    if dt is None:
        return "-"

    return dt.strftime(
        "%m-%d %H:%M:%S"
    )


def server_time_string(
    value: Any,
) -> str | None:
    return epoch_ms_to_et_string(
        value
    )


def force_flush(file) -> None:
    file.flush()

    try:
        os.fsync(
            file.fileno()
        )
    except OSError:
        pass


def field(
    state: dict[str, Any],
    number: int | str,
) -> Any:
    return state.get(
        str(number)
    )


def changed_field_description(
    content: dict[str, Any],
) -> str:
    result: list[str] = []

    for key in content:
        if key.isdigit():
            name = FIELD_NAMES.get(
                key,
                "unknown",
            )

            result.append(
                f"{key}:{name}"
            )

    return ";".join(result)


def stream_csv_row(
    *,
    update_number: int,
    received_at: datetime,
    server_timestamp: Any,
    command: Any,
    symbol: str,
    changed_content: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:

    quote_time = field(
        state,
        34,
    )

    trade_time = field(
        state,
        35,
    )

    regular_trade_time = field(
        state,
        36,
    )

    bid_time = field(
        state,
        37,
    )

    ask_time = field(
        state,
        38,
    )

    return {
        "update_number":
            update_number,

        "received_at_et":
            received_at.isoformat(),

        "received_at_utc":
            received_at.astimezone(
                timezone.utc
            ).isoformat(),

        "server_timestamp_ms":
            server_timestamp,

        "server_timestamp_et":
            server_time_string(
                server_timestamp
            ),

        "command":
            command,

        "symbol":
            symbol,

        "changed_fields":
            changed_field_description(
                changed_content
            ),

        "delayed":
            state.get("delayed"),

        "assetMainType":
            state.get("assetMainType"),

        "assetSubType":
            state.get("assetSubType"),

        "cusip":
            state.get("cusip"),

        "bidPrice":
            field(state, 1),

        "askPrice":
            field(state, 2),

        "lastPrice":
            field(state, 3),

        "bidSize":
            field(state, 4),

        "askSize":
            field(state, 5),

        "totalVolume":
            field(state, 8),

        "lastSize":
            field(state, 9),

        "highPrice":
            field(state, 10),

        "lowPrice":
            field(state, 11),

        "closePrice":
            field(state, 12),

        "openPrice":
            field(state, 17),

        "regularMarketQuote":
            field(state, 27),

        "regularMarketTrade":
            field(state, 28),

        "regularMarketLastPrice":
            field(state, 29),

        "regularMarketLastSize":
            field(state, 30),

        "regularMarketNetChange":
            field(state, 31),

        "securityStatus":
            field(state, 32),

        "markPrice":
            field(state, 33),

        "quoteTime_ms":
            quote_time,

        "quoteTime_et":
            epoch_ms_to_et_string(
                quote_time
            ),

        "tradeTime_ms":
            trade_time,

        "tradeTime_et":
            epoch_ms_to_et_string(
                trade_time
            ),

        "regularMarketTradeTime_ms":
            regular_trade_time,

        "regularMarketTradeTime_et":
            epoch_ms_to_et_string(
                regular_trade_time
            ),

        "bidTime_ms":
            bid_time,

        "bidTime_et":
            epoch_ms_to_et_string(
                bid_time
            ),

        "askTime_ms":
            ask_time,

        "askTime_et":
            epoch_ms_to_et_string(
                ask_time
            ),

        "askMICId":
            field(state, 39),

        "bidMICId":
            field(state, 40),

        "lastMICId":
            field(state, 41),

        "netPercentChange":
            field(state, 42),

        "regularMarketPercentChange":
            field(state, 43),

        "markPriceNetChange":
            field(state, 44),

        "markPricePercentChange":
            field(state, 45),

        "postMarketNetChange":
            field(state, 50),

        "postMarketPercentChange":
            field(state, 51),
    }


def rest_csv_row(
    *,
    sample_number: int,
    observed_at: datetime,
    symbol: str,
    http_status: int,
    item: dict[str, Any],
) -> dict[str, Any]:

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

    extended_quote_time = nested(
        item,
        "extended",
        "quoteTime",
    )

    extended_trade_time = nested(
        item,
        "extended",
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
            extended_quote_time,

        "extended_quoteTime_et":
            epoch_ms_to_et_string(
                extended_quote_time
            ),

        "extended_tradeTime_ms":
            extended_trade_time,

        "extended_tradeTime_et":
            epoch_ms_to_et_string(
                extended_trade_time
            ),

        "regular_lastPrice":
            nested(
                item,
                "regular",
                "regularMarketLastPrice",
            ),

        "regular_lastSize":
            nested(
                item,
                "regular",
                "regularMarketLastSize",
            ),

        "regular_tradeTime_ms":
            regular_trade_time,

        "regular_tradeTime_et":
            epoch_ms_to_et_string(
                regular_trade_time
            ),

        "sharesOutstanding":
            nested(
                item,
                "fundamental",
                "sharesOutstanding",
            ),
    }


def print_status(
    *,
    now: datetime,
    streamer: Any,
    symbols: list[str],
    states: dict[str, dict[str, Any]],
    latest_rest: dict[str, dict[str, Any]],
    message_count: int,
    update_count: int,
    rest_sample_count: int,
) -> None:

    print()
    print(
        f"Status {now:%Y-%m-%d %H:%M:%S %Z}  "
        f"stream_active={streamer.active}  "
        f"messages={message_count}  "
        f"updates={update_count}  "
        f"REST={rest_sample_count}"
    )

    for symbol in symbols:
        stream_state = states.get(
            symbol,
            {},
        )

        rest_item = latest_rest.get(
            symbol,
            {},
        )

        stream_volume = field(
            stream_state,
            8,
        )

        stream_last = field(
            stream_state,
            3,
        )

        stream_trade_time = field(
            stream_state,
            35,
        )

        rest_quote_volume = nested(
            rest_item,
            "quote",
            "totalVolume",
        )

        rest_ext_volume = nested(
            rest_item,
            "extended",
            "totalVolume",
        )

        rest_ext_trade = nested(
            rest_item,
            "extended",
            "tradeTime",
        )

        print(
            f"  {symbol:<6} "
            f"streamVol={str(stream_volume):>12}  "
            f"streamLast={str(stream_last):>10}  "
            f"streamTrade={short_time(stream_trade_time):<17}  "
            f"RESTqVol={str(rest_quote_volume):>12}  "
            f"RESTextVol={str(rest_ext_volume):>8}  "
            f"RESTextTrade={short_time(rest_ext_trade)}"
        )


def main() -> int:
    args = parse_args()

    if (
        args.duration is not None
        and args.duration <= 0
    ):
        raise SystemExit(
            "--duration must be greater than zero."
        )

    if (
        args.max_updates is not None
        and args.max_updates <= 0
    ):
        raise SystemExit(
            "--max-updates must be greater than zero."
        )

    if args.rest_interval < 0:
        raise SystemExit(
            "--rest-interval cannot be negative."
        )

    if args.report_interval <= 0:
        raise SystemExit(
            "--report-interval must be greater than zero."
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
        / "levelone_probe"
        / run_stamp
    )

    run_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    stream_raw_path = (
        run_dir
        / "stream_raw_messages.jsonl"
    )

    stream_updates_path = (
        run_dir
        / "stream_updates.jsonl"
    )

    stream_csv_path = (
        run_dir
        / "stream_state.csv"
    )

    rest_raw_path = (
        run_dir
        / "rest_quotes.jsonl"
    )

    rest_csv_path = (
        run_dir
        / "rest_quotes.csv"
    )

    error_path = (
        run_dir
        / "errors.log"
    )

    manifest_path = (
        run_dir
        / "manifest.json"
    )

    manifest: dict[str, Any] = {
        "probe":
            "levelone_equities",

        "started_at_et":
            started_at.isoformat(),

        "completed_at_et":
            None,

        "symbols":
            symbols,

        "stream_fields":
            FIELD_NAMES,

        "stop_at_et":
            (
                stop_at.isoformat()
                if stop_at is not None
                else None
            ),

        "duration_seconds":
            args.duration,

        "max_updates":
            args.max_updates,

        "rest_interval_seconds":
            args.rest_interval,

        "report_interval_seconds":
            args.report_interval,

        "ping_interval_seconds":
            args.ping_interval,

        "stream_messages":
            0,

        "stream_updates":
            0,

        "rest_samples":
            0,

        "rest_successes":
            0,

        "output_directory":
            str(run_dir),
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
    print("Schwab LEVELONE_EQUITIES probe")
    print("=" * 79)
    print(
        "Symbols          : "
        + " ".join(symbols)
    )
    print(
        "Stream fields    : "
        "0 through 51"
    )
    print(
        f"REST interval    : "
        f"{args.rest_interval:g} seconds"
    )
    print(
        f"Report interval  : "
        f"{args.report_interval:g} seconds"
    )
    print(
        f"Started          : "
        f"{started_at:%Y-%m-%d %H:%M:%S %Z}"
    )

    if stop_at is not None:
        print(
            f"Automatic stop   : "
            f"{stop_at:%Y-%m-%d %H:%M:%S %Z}"
        )
    else:
        print(
            "Automatic stop   : none"
        )

    if args.duration is not None:
        print(
            f"Duration         : "
            f"{args.duration:g} seconds"
        )
    else:
        print(
            "Duration         : none"
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
    streamer = None

    #
    # The streaming receiver runs in Schwabdev's streaming thread.
    # Keep it deliberately cheap: timestamp and enqueue only.
    #
    # All parsing and file I/O happen in this program's main thread.
    #

    message_queue: queue.Queue[
        tuple[datetime, str]
    ] = queue.Queue()

    def receiver(
        message: str,
    ) -> None:
        message_queue.put(
            (
                datetime.now(ET),
                message,
            )
        )

    states: dict[
        str,
        dict[str, Any],
    ] = {}

    latest_rest: dict[
        str,
        dict[str, Any],
    ] = {}

    message_count = 0
    update_count = 0
    rest_sample_count = 0
    rest_success_count = 0

    try:
        client = make_secure_schwab_client(
            ecfg_path,
            password,
            timeout=args.timeout,
            call_on_auth=
                console_auth_callback,
        )

        streamer = schwabdev.Stream(
            client
        )

        with (
            stream_raw_path.open(
                "a",
                encoding="utf-8",
            ) as stream_raw_file,
            stream_updates_path.open(
                "a",
                encoding="utf-8",
            ) as stream_updates_file,
            stream_csv_path.open(
                "a",
                newline="",
                encoding="utf-8",
            ) as stream_csv_file,
            rest_raw_path.open(
                "a",
                encoding="utf-8",
            ) as rest_raw_file,
            rest_csv_path.open(
                "a",
                newline="",
                encoding="utf-8",
            ) as rest_csv_file,
            error_path.open(
                "a",
                encoding="utf-8",
            ) as error_file,
        ):
            stream_writer = csv.DictWriter(
                stream_csv_file,
                fieldnames=
                    STREAM_CSV_FIELDS,
            )

            rest_writer = csv.DictWriter(
                rest_csv_file,
                fieldnames=
                    REST_CSV_FIELDS,
            )

            if stream_csv_file.tell() == 0:
                stream_writer.writeheader()
                force_flush(
                    stream_csv_file
                )

            if rest_csv_file.tell() == 0:
                rest_writer.writeheader()
                force_flush(
                    rest_csv_file
                )

            print()
            print(
                "Starting Schwab stream..."
            )

            streamer.start(
                receiver=receiver,
                daemon=True,
                ping_interval=
                    args.ping_interval,
            )

            subscription = (
                streamer.level_one_equities(
                    symbols,
                    STREAM_FIELDS,
                    command="SUBS",
                )
            )

            streamer.send(
                subscription
            )

            print(
                "LEVELONE_EQUITIES "
                "subscription submitted."
            )

            start_monotonic = (
                time.monotonic()
            )

            next_rest = (
                start_monotonic
            )

            next_report = (
                start_monotonic
                + args.report_interval
            )

            while True:
                now = datetime.now(ET)
                mono = time.monotonic()

                if (
                    stop_at is not None
                    and now >= stop_at
                ):
                    print()
                    print(
                        "Reached automatic "
                        "stop time."
                    )
                    break

                if (
                    args.duration is not None
                    and mono - start_monotonic
                    >= args.duration
                ):
                    print()
                    print(
                        "Reached requested "
                        "duration."
                    )
                    break

                if (
                    args.max_updates
                    is not None
                    and update_count
                    >= args.max_updates
                ):
                    print()
                    print(
                        "Reached maximum "
                        "stream update count."
                    )
                    break

                #
                # Drain one queued streaming message,
                # waiting briefly if the queue is empty.
                #

                try:
                    (
                        received_at,
                        raw_message,
                    ) = message_queue.get(
                        timeout=0.25
                    )

                except queue.Empty:
                    raw_message = None
                    received_at = None

                if raw_message is not None:
                    message_count += 1

                    raw_record = {
                        "message_number":
                            message_count,

                        "received_at_et":
                            received_at.isoformat(),

                        "raw_message":
                            raw_message,
                    }

                    stream_raw_file.write(
                        json.dumps(
                            raw_record,
                            sort_keys=True,
                        )
                        + "\n"
                    )

                    force_flush(
                        stream_raw_file
                    )

                    try:
                        parsed = json.loads(
                            raw_message
                        )

                    except json.JSONDecodeError as exc:
                        error_file.write(
                            f"{received_at.isoformat()} "
                            f"JSONDecodeError: "
                            f"{exc}\n"
                        )

                        force_flush(
                            error_file
                        )

                        parsed = None

                    if isinstance(
                        parsed,
                        dict,
                    ):
                        for data_item in parsed.get(
                            "data",
                            [],
                        ):
                            if (
                                data_item.get(
                                    "service"
                                )
                                !=
                                "LEVELONE_EQUITIES"
                            ):
                                continue

                            server_timestamp = (
                                data_item.get(
                                    "timestamp"
                                )
                            )

                            command = (
                                data_item.get(
                                    "command"
                                )
                            )

                            for content in data_item.get(
                                "content",
                                [],
                            ):
                                symbol = (
                                    content.get(
                                        "key"
                                    )
                                    or
                                    content.get(
                                        "0"
                                    )
                                )

                                if symbol is None:
                                    continue

                                symbol = str(
                                    symbol
                                ).upper()

                                state = states.setdefault(
                                    symbol,
                                    {},
                                )

                                #
                                # Incremental-stream merge:
                                # newly supplied fields overwrite
                                # their previous values.
                                #
                                state.update(
                                    content
                                )

                                update_count += 1

                                merged_record = {
                                    "update_number":
                                        update_count,

                                    "received_at_et":
                                        received_at.isoformat(),

                                    "server_timestamp_ms":
                                        server_timestamp,

                                    "server_timestamp_et":
                                        server_time_string(
                                            server_timestamp
                                        ),

                                    "command":
                                        command,

                                    "symbol":
                                        symbol,

                                    "changed":
                                        content,

                                    "merged_state":
                                        dict(state),
                                }

                                stream_updates_file.write(
                                    json.dumps(
                                        merged_record,
                                        sort_keys=True,
                                    )
                                    + "\n"
                                )

                                force_flush(
                                    stream_updates_file
                                )

                                stream_writer.writerow(
                                    stream_csv_row(
                                        update_number=
                                            update_count,
                                        received_at=
                                            received_at,
                                        server_timestamp=
                                            server_timestamp,
                                        command=
                                            command,
                                        symbol=
                                            symbol,
                                        changed_content=
                                            content,
                                        state=
                                            state,
                                    )
                                )

                                force_flush(
                                    stream_csv_file
                                )

                #
                # REST quote snapshot using the SAME client.
                #

                mono = time.monotonic()

                if (
                    args.rest_interval > 0
                    and mono >= next_rest
                ):
                    rest_sample_count += 1

                    requested_at = (
                        datetime.now(ET)
                    )

                    try:
                        response = client.quotes(
                            symbols,
                            fields="all",
                        )

                        received_at = (
                            datetime.now(ET)
                        )

                        rest_record: dict[
                            str,
                            Any,
                        ] = {
                            "sample_number":
                                rest_sample_count,

                            "requested_at_et":
                                requested_at.isoformat(),

                            "received_at_et":
                                received_at.isoformat(),

                            "http_status":
                                response.status_code,
                        }

                        if response.ok:
                            data = (
                                response.json()
                            )

                            rest_record[
                                "response"
                            ] = data

                            latest_rest = data

                            for symbol in symbols:
                                rest_writer.writerow(
                                    rest_csv_row(
                                        sample_number=
                                            rest_sample_count,
                                        observed_at=
                                            received_at,
                                        symbol=
                                            symbol,
                                        http_status=
                                            response.status_code,
                                        item=
                                            data.get(
                                                symbol,
                                                {},
                                            ),
                                    )
                                )

                            force_flush(
                                rest_csv_file
                            )

                            rest_success_count += 1

                        else:
                            rest_record[
                                "response_text"
                            ] = response.text

                            error_file.write(
                                f"{received_at.isoformat()} "
                                f"REST sample="
                                f"{rest_sample_count} "
                                f"HTTP="
                                f"{response.status_code} "
                                f"{response.text}\n"
                            )

                            force_flush(
                                error_file
                            )

                        rest_raw_file.write(
                            json.dumps(
                                rest_record,
                                sort_keys=True,
                            )
                            + "\n"
                        )

                        force_flush(
                            rest_raw_file
                        )

                    except Exception as exc:
                        failed_at = (
                            datetime.now(ET)
                        )

                        error_file.write(
                            f"{failed_at.isoformat()} "
                            f"REST sample="
                            f"{rest_sample_count} "
                            f"{type(exc).__name__}: "
                            f"{exc}\n"
                        )

                        force_flush(
                            error_file
                        )

                    next_rest += (
                        args.rest_interval
                    )

                    #
                    # If execution fell far behind,
                    # resume from now rather than
                    # issuing rapid catch-up requests.
                    #
                    if (
                        next_rest
                        < time.monotonic()
                        - args.rest_interval
                    ):
                        next_rest = (
                            time.monotonic()
                            + args.rest_interval
                        )

                #
                # Periodic console status.
                #

                mono = time.monotonic()

                if mono >= next_report:
                    print_status(
                        now=datetime.now(ET),
                        streamer=streamer,
                        symbols=symbols,
                        states=states,
                        latest_rest=
                            latest_rest,
                        message_count=
                            message_count,
                        update_count=
                            update_count,
                        rest_sample_count=
                            rest_sample_count,
                    )

                    next_report += (
                        args.report_interval
                    )

                    if (
                        next_report
                        < time.monotonic()
                    ):
                        next_report = (
                            time.monotonic()
                            + args.report_interval
                        )

    except KeyboardInterrupt:
        print()
        print(
            "Stopped by user."
        )

    finally:
        completed_at = (
            datetime.now(ET)
        )

        if streamer is not None:
            try:
                streamer.stop()
            except Exception as exc:
                print(
                    "Warning while stopping "
                    f"stream: {exc}"
                )

        if client is not None:
            try:
                client.close()
            except Exception as exc:
                print(
                    "Warning while closing "
                    f"client: {exc}"
                )

        manifest[
            "completed_at_et"
        ] = completed_at.isoformat()

        manifest[
            "stream_messages"
        ] = message_count

        manifest[
            "stream_updates"
        ] = update_count

        manifest[
            "rest_samples"
        ] = rest_sample_count

        manifest[
            "rest_successes"
        ] = rest_success_count

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
        print(
            "LEVELONE_EQUITIES "
            "probe finished"
        )
        print("=" * 79)
        print(
            f"Completed        : "
            f"{completed_at:%Y-%m-%d %H:%M:%S %Z}"
        )
        print(
            f"Stream messages  : "
            f"{message_count}"
        )
        print(
            f"Equity updates   : "
            f"{update_count}"
        )
        print(
            f"REST samples     : "
            f"{rest_sample_count}"
        )
        print(
            f"REST successes   : "
            f"{rest_success_count}"
        )
        print(
            f"Output directory : "
            f"{run_dir}"
        )
        print()
        print(
            f"Stream raw       : "
            f"{stream_raw_path}"
        )
        print(
            f"Stream updates   : "
            f"{stream_updates_path}"
        )
        print(
            f"Stream CSV       : "
            f"{stream_csv_path}"
        )
        print(
            f"REST raw         : "
            f"{rest_raw_path}"
        )
        print(
            f"REST CSV         : "
            f"{rest_csv_path}"
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
