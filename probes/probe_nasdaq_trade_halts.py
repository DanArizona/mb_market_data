from __future__ import annotations

import argparse
import csv
import json
import sys
import xml.etree.ElementTree as ET

from collections import Counter
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path


# Allow this probe to run directly from the repository root
# without requiring mb_market_data to be installed first.
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


from mb_market_data.nasdaq_halts import (  # noqa: E402
    HaltRecord,
    fetch_trade_halts,
    normalize_nasdaq_xml,
)


VOLATILITY_REASON_CODES = {"LUDP", "LUDS", "M"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe the Nasdaq Trader trade-halt RSS feed."
    )

    parser.add_argument(
        "--date",
        type=parse_iso_date,
        metavar="YYYY-MM-DD",
        help=(
            "Fetch halts whose initial halt date is this date. "
            "If omitted, fetch the current trade-halt feed."
        ),
    )

    return parser.parse_args()


def parse_iso_date(value: str) -> date:
    try:
        return datetime.strptime(
            value,
            "%Y-%m-%d",
        ).date()

    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid date {value!r}; expected YYYY-MM-DD"
        ) from exc


def make_output_dir(
    retrieved_at: datetime,
    halt_date: date | None,
) -> Path:

    timestamp = retrieved_at.strftime(
        "%Y-%m-%d-%H-%M-%S"
    )

    if halt_date is None:
        suffix = "CURRENT"
    else:
        suffix = f"HIST-{halt_date.isoformat()}"

    output_dir = (
        REPO_ROOT
        / "output"
        / "nasdaq_trade_halts"
        / f"{timestamp}-{suffix}"
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    return output_dir


def save_raw_xml(
    output_dir: Path,
    raw_xml: bytes,
) -> Path:

    path = output_dir / "raw.xml"

    path.write_bytes(
        raw_xml
    )

    return path


def save_json(
    output_dir: Path,
    records: tuple[HaltRecord, ...],
) -> Path:

    path = output_dir / "normalized.json"

    data = [
        asdict(record)
        for record in records
    ]

    path.write_text(
        json.dumps(
            data,
            indent=2,
        ),
        encoding="utf-8",
    )

    return path


def save_csv(
    output_dir: Path,
    records: tuple[HaltRecord, ...],
) -> Path:

    path = output_dir / "normalized.csv"

    fieldnames = list(
        HaltRecord.__dataclass_fields__.keys()
    )

    with path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
        )

        writer.writeheader()

        for record in records:
            writer.writerow(
                asdict(record)
            )

    return path


def print_first_item_fields(
    raw_xml: bytes,
) -> None:

    normalized_xml = normalize_nasdaq_xml(
        raw_xml
    )

    root = ET.fromstring(
        normalized_xml
    )

    item = root.find(
        "./channel/item"
    )

    print()
    print("FIRST RSS ITEM FIELDS")
    print("-" * 70)

    if item is None:
        print("(no items)")
        return

    for child in item:
        text = "".join(
            child.itertext()
        )

        print(
            f"{child.tag:24} "
            f"{text!r}"
        )


def build_summary(
    records: tuple[HaltRecord, ...],
) -> Counter[tuple[str, str]]:

    return Counter(
        (
            record.reason_code,
            record.market,
        )
        for record in records
    )


def print_records(
    records: tuple[HaltRecord, ...],
) -> None:

    print()
    print("HALT RECORDS")
    print("-" * 110)

    if not records:
        print("(none)")
        return

    print(
        f"{'Date':10}  "
        f"{'Time':12}  "
        f"{'Symbol':8}  "
        f"{'Market':12}  "
        f"{'Mkt':4}  "
        f"{'Reason':8}  "
        f"{'Threshold':12}"
    )

    print("-" * 110)

    for record in records:
        print(
            f"{record.halt_date:10}  "
            f"{record.halt_time:12}  "
            f"{record.symbol:8}  "
            f"{record.market:12}  "
            f"{record.market_code:4}  "
            f"{record.reason_code:8}  "
            f"{record.pause_threshold_price:12}"
        )


def print_summary(
    records: tuple[HaltRecord, ...],
) -> None:

    summary = build_summary(
        records
    )

    print()
    print("REASON / MARKET SUMMARY")
    print("-" * 45)

    print(
        f"{'Reason':10} "
        f"{'Market':15} "
        f"{'Count':>8}"
    )

    print("-" * 45)

    for (reason, market), count in sorted(
        summary.items()
    ):
        print(
            f"{reason:10} "
            f"{market:15} "
            f"{count:8}"
        )


def print_volatility_records(
    records: tuple[HaltRecord, ...],
) -> None:

    matching = [
        record
        for record in records
        if record.reason_code
        in VOLATILITY_REASON_CODES
    ]

    print()
    print("VOLATILITY-RELATED RECORDS")
    print(
        "Codes examined: "
        f"{sorted(VOLATILITY_REASON_CODES)}"
    )
    print("-" * 110)

    if not matching:
        print("(none)")
        return

    for record in matching:
        print(
            f"{record.halt_date} "
            f"{record.halt_time:12} "
            f"{record.symbol:8} "
            f"{record.market:12} "
            f"{record.market_code:4} "
            f"{record.reason_code:6} "
            f"{record.issue_name}"
        )


def main() -> int:
    args = parse_args()

    halt_date: date | None = args.date

    print("Nasdaq Trade Halt RSS Probe")
    print("=" * 70)

    try:
        feed = fetch_trade_halts(
            halt_date=halt_date,
        )

    except Exception as exc:
        print(
            f"ERROR fetching/parsing Nasdaq RSS feed: "
            f"{exc}",
            file=sys.stderr,
        )
        return 1

    output_dir = make_output_dir(
        feed.retrieved_at_et,
        halt_date,
    )

    print(
        f"Mode          : "
        f"{feed.retrieval_mode}"
    )

    if halt_date is not None:
        print(
            f"Halt date     : "
            f"{halt_date.isoformat()}"
        )

    print(
        f"Retrieved ET  : "
        f"{feed.retrieved_at_et.isoformat()}"
    )

    print(
        f"URL           : "
        f"{feed.url}"
    )

    print(
        f"Output folder : "
        f"{output_dir}"
    )

    raw_path = save_raw_xml(
        output_dir,
        feed.raw_xml,
    )

    print()
    print(
        f"Bytes received: "
        f"{len(feed.raw_xml):,}"
    )

    print(
        f"Raw XML       : "
        f"{raw_path}"
    )

    print_first_item_fields(
        feed.raw_xml
    )

    json_path = save_json(
        output_dir,
        feed.records,
    )

    csv_path = save_csv(
        output_dir,
        feed.records,
    )

    print(
        f"Records parsed: "
        f"{len(feed.records)}"
    )

    print(
        f"JSON           : "
        f"{json_path}"
    )

    print(
        f"CSV            : "
        f"{csv_path}"
    )

    ludp_count = sum(
        record.reason_code == "LUDP"
        for record in feed.records
    )

    m_count = sum(
        record.reason_code == "M"
        for record in feed.records
    )

    non_nasdaq_count = sum(
        bool(record.market)
        and record.market.upper() != "NASDAQ"
        for record in feed.records
    )

    print()
    print("Quick counts")
    print("-" * 30)

    print(
        f"LUDP          : "
        f"{ludp_count}"
    )

    print(
        f"M             : "
        f"{m_count}"
    )

    print(
        f"Non-NASDAQ    : "
        f"{non_nasdaq_count}"
    )

    print_records(
        feed.records
    )

    print_summary(
        feed.records
    )

    print_volatility_records(
        feed.records
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
