from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


BASE_URL = "https://www.nasdaqtrader.com/rss.aspx"
ET_ZONE = ZoneInfo("America/New_York")

NASDAQ_MARKET_CODES = {"Q", "G", "S"}
NON_NASDAQ_MARKET_CODES = {"A", "N", "P", "Z", "V"}
VOLATILITY_REASON_CODES = {"LUDP", "M"}

@dataclass(frozen=True)
class HaltRecord:
    halt_date: str
    halt_time: str
    symbol: str
    issue_name: str
    market: str
    market_code: str
    reason_code: str
    pause_threshold_price: str
    resumption_date: str
    resumption_quote_time: str
    resumption_trade_time: str
    retrieved_at_et: str
    retrieval_mode: str


@dataclass(frozen=True)
class HaltFeed:
    url: str
    retrieved_at_et: datetime
    retrieval_mode: str
    requested_halt_date: date | None
    raw_xml: bytes
    records: tuple[HaltRecord, ...]


def build_url(halt_date: date | None = None) -> str:
    """
    Build the Nasdaq Trader trade-halt RSS URL.

    If halt_date is None, request the current feed.

    Otherwise request Nasdaq's historical halt feed for the
    supplied halt date.
    """
    params = {
        "feed": "tradehalts",
    }

    if halt_date is not None:
        params["haltdate"] = halt_date.strftime("%m%d%Y")

    return f"{BASE_URL}?{urlencode(params)}"


def fetch_rss(
    url: str,
    *,
    timeout: float = 30.0,
) -> bytes:
    """
    Fetch raw Nasdaq trade-halt RSS XML.
    """
    request = Request(
        url,
        headers={
            "User-Agent": (
                "mb_market_data Nasdaq trade-halt client/0.1"
            ),
            "Accept": (
                "application/rss+xml, "
                "application/xml, "
                "text/xml, "
                "*/*"
            ),
        },
    )

    with urlopen(
        request,
        timeout=timeout,
    ) as response:
        return response.read()


def normalize_nasdaq_xml(raw_xml: bytes) -> str:
    """
    Prepare Nasdaq RSS XML for ElementTree parsing.

    Nasdaq RSS has historically used item fields such as:

        ndaq:HaltDate
        ndaq:IssueSymbol
        ndaq:ReasonCode

    Some responses do not provide a conventional XML namespace
    declaration for the ndaq prefix.

    The original raw bytes are never modified.  This function
    returns a parsing copy with the ndaq prefix removed.
    """
    text = raw_xml.decode(
        "utf-8-sig",
        errors="replace",
    )

    return re.sub(
        r"(<\/?)ndaq:",
        r"\1",
        text,
    )


def field_text(
    item: ET.Element,
    field_name: str,
) -> str:
    """
    Return normalized text from one RSS item field.

    Embedded whitespace is collapsed to a single space.
    """
    element = item.find(field_name)

    if element is None:
        return ""

    text = "".join(
        element.itertext()
    )

    return " ".join(
        text.split()
    )


def normalize_halt_time(value: str) -> str:
    """
    Remove whitespace inserted between Nasdaq halt-time
    seconds and fractional seconds.

    Example:

        '09:30:37 .266'

    becomes:

        '09:30:37.266'
    """
    return "".join(
        value.split()
    )


def parse_market(
    item: ET.Element,
) -> tuple[str, str]:
    """
    Normalize the two Nasdaq market representations observed
    in the current and historical RSS feeds.

    Current feed example:

        <Market>NASDAQ</Market>
        <Market>NYSE</Market>
        <Market>NYSE Arca</Market>
        <Market>AMEX</Market>

    Historical feed example:

        <Mkt>Q</Mkt>
        <Mkt>N</Mkt>
        <Mkt>A</Mkt>
        <Mkt>Z</Mkt>

    Returns:

        (market, market_code)

    For the current feed, Nasdaq's full Market text is
    preserved and market_code is normally empty.

    For the historical feed, the one-character Mkt code is
    preserved.  The broad market classification is normalized
    to NASDAQ, Non-NASDAQ, or UNKNOWN.
    """
    market = field_text(
        item,
        "Market",
    )

    market_code = field_text(
        item,
        "Mkt",
    ).upper()

    # Current-feed representation.
    if market:
        return market.strip(), market_code

    # Historical-feed representation.
    if market_code:
        if market_code in NASDAQ_MARKET_CODES:
            return "NASDAQ", market_code

        if market_code in NON_NASDAQ_MARKET_CODES:
            return "Non-NASDAQ", market_code

        return "UNKNOWN", market_code

    return "", ""


def parse_records(
    raw_xml: bytes,
    *,
    retrieved_at_et: datetime,
    retrieval_mode: str,
) -> list[HaltRecord]:
    """
    Parse raw Nasdaq trade-halt RSS into normalized HaltRecord
    objects.
    """
    normalized_xml = normalize_nasdaq_xml(
        raw_xml
    )

    root = ET.fromstring(
        normalized_xml
    )

    records: list[HaltRecord] = []

    for item in root.findall(
        "./channel/item"
    ):
        market, market_code = parse_market(
            item
        )

        record = HaltRecord(
            halt_date=field_text(
                item,
                "HaltDate",
            ),
            halt_time=normalize_halt_time(
                field_text(
                    item,
                    "HaltTime",
                )
            ),
            symbol=field_text(
                item,
                "IssueSymbol",
            ),
            issue_name=field_text(
                item,
                "IssueName",
            ),
            market=market,
            market_code=market_code,
            reason_code=field_text(
                item,
                "ReasonCode",
            ),
            pause_threshold_price=field_text(
                item,
                "PauseThresholdPrice",
            ),
            resumption_date=field_text(
                item,
                "ResumptionDate",
            ),
            resumption_quote_time=field_text(
                item,
                "ResumptionQuoteTime",
            ),
            resumption_trade_time=field_text(
                item,
                "ResumptionTradeTime",
            ),
            retrieved_at_et=(
                retrieved_at_et.isoformat()
            ),
            retrieval_mode=retrieval_mode,
        )

        records.append(record)

    return records


def records_for_date(
    records: tuple[HaltRecord, ...] | list[HaltRecord],
    target_date: date,
) -> tuple[HaltRecord, ...]:
    """
    Return only halt records whose Nasdaq HaltDate matches
    target_date.

    Nasdaq supplies HaltDate as MM/DD/YYYY.
    """
    target = target_date.strftime("%m/%d/%Y")

    return tuple(
        record
        for record in records
        if record.halt_date == target
    )


def records_for_today_et(
    records: tuple[HaltRecord, ...] | list[HaltRecord],
    *,
    now_et: datetime | None = None,
) -> tuple[HaltRecord, ...]:
    """
    Return halt records whose HaltDate is today's ET date.
    """
    if now_et is None:
        now_et = datetime.now(ET_ZONE)

    return records_for_date(
        records,
        now_et.date(),
    )


def records_with_reason_codes(
    records: tuple[HaltRecord, ...] | list[HaltRecord],
    reason_codes: set[str],
) -> tuple[HaltRecord, ...]:
    """
    Return halt records whose ReasonCode is in reason_codes.
    """
    normalized_codes = {
        code.upper()
        for code in reason_codes
    }

    return tuple(
        record
        for record in records
        if record.reason_code.upper() in normalized_codes
    )


def halt_event_key(record: HaltRecord) -> tuple[str, str, str, str]:
    """
    Return a stable identity for one halt event.

    A symbol may halt multiple times in one day, so symbol alone
    is not sufficient.
    """
    return (
        record.halt_date,
        record.halt_time,
        record.symbol,
        record.reason_code,
    )


def unique_halt_events(
    records: tuple[HaltRecord, ...] | list[HaltRecord],
) -> tuple[HaltRecord, ...]:
    """
    Remove duplicate halt-event records while preserving order.
    """
    seen: set[tuple[str, str, str, str]] = set()
    unique: list[HaltRecord] = []

    for record in records:
        key = halt_event_key(record)

        if key in seen:
            continue

        seen.add(key)
        unique.append(record)

    return tuple(unique)


def unique_symbols(
    records: tuple[HaltRecord, ...] | list[HaltRecord],
) -> tuple[str, ...]:
    """
    Return unique symbols from halt records, preserving
    the order in which each symbol first appears.
    """
    seen: set[str] = set()
    symbols: list[str] = []

    for record in records:
        symbol = record.symbol.strip()

        if not symbol:
            continue

        if symbol in seen:
            continue

        seen.add(symbol)
        symbols.append(symbol)

    return tuple(symbols)


def volatility_symbols_for_today(
    records: tuple[HaltRecord, ...] | list[HaltRecord],
    *,
    now_et: datetime | None = None,
) -> tuple[str, ...]:
    """
    Return unique symbols from today's ET volatility-pause events.

    Current operational reason codes:
        LUDP
        M
    """
    today = records_for_today_et(
        records,
        now_et=now_et,
    )

    volatility = records_with_reason_codes(
        today,
        VOLATILITY_REASON_CODES,
    )

    unique_events = unique_halt_events(
        volatility,
    )

    return unique_symbols(
        unique_events,
    )


def fetch_trade_halts(
    halt_date: date | None = None,
    *,
    timeout: float = 30.0,
) -> HaltFeed:
    """
    Fetch and parse Nasdaq trade halts.

    halt_date=None:
        fetch the current RSS feed.

    halt_date=<date>:
        fetch the historical RSS feed for that halt date.

    This function performs acquisition and normalization only.
    It does not:

      * filter records to today's date;
      * filter reason codes;
      * deduplicate halt events;
      * persist records;
      * modify a ThinkOrSwim Watchlist.
    """
    if halt_date is None:
        retrieval_mode = "CURRENT"
    else:
        retrieval_mode = "HISTORICAL"

    url = build_url(
        halt_date
    )

    raw_xml = fetch_rss(
        url,
        timeout=timeout,
    )

    retrieved_at_et = datetime.now(
        ET_ZONE
    )

    records = parse_records(
        raw_xml,
        retrieved_at_et=retrieved_at_et,
        retrieval_mode=retrieval_mode,
    )

    return HaltFeed(
        url=url,
        retrieved_at_et=retrieved_at_et,
        retrieval_mode=retrieval_mode,
        requested_halt_date=halt_date,
        raw_xml=raw_xml,
        records=tuple(records),
    )
