"""Acquire and normalize the Nasdaq Trader symbol directory."""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from mb_market_data.daily_universe import sha256_file


NASDAQ_LISTED_URL = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
)
OTHER_LISTED_URL = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
)

NORMALIZED_FIELDS = [
    "symbol",
    "security_name",
    "source",
    "listing_exchange_code",
    "listing_exchange_name",
    "market_category",
    "test_issue",
    "financial_status",
    "etf",
    "round_lot_size",
    "cqs_symbol",
    "nasdaq_symbol",
]

EXCHANGE_NAMES = {
    "A": "NYSE American",
    "N": "NYSE",
    "P": "NYSE Arca",
    "Q": "NASDAQ",
    "V": "IEX",
    "Z": "Cboe BZX",
}


@dataclass(frozen=True)
class ParsedDirectory:
    rows: tuple[dict[str, str], ...]
    file_creation_time: str


@dataclass(frozen=True)
class SymbolDirectorySnapshot:
    nasdaq_raw: bytes
    other_raw: bytes
    nasdaq_file_creation_time: str
    other_file_creation_time: str
    normalized_rows: tuple[dict[str, str], ...]

    @property
    def candidate_rows(self) -> tuple[dict[str, str], ...]:
        return tuple(
            row
            for row in self.normalized_rows
            if row["test_issue"] != "Y" and row["etf"] != "Y"
        )


def fetch_directory(url: str, *, timeout: float = 30.0) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "mb_market_data symbol-directory client/0.1",
            "Accept": "text/plain,*/*",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def _parse_pipe_directory(
    raw: bytes,
    *,
    required_fields: set[str],
    symbol_field: str,
) -> ParsedDirectory:
    text = raw.decode("utf-8-sig", errors="strict")
    reader = csv.DictReader(io.StringIO(text), delimiter="|")
    fields = set(reader.fieldnames or ())
    missing = sorted(required_fields - fields)
    if missing:
        raise ValueError(
            "Nasdaq directory is missing required fields: "
            + ", ".join(missing)
        )

    rows: list[dict[str, str]] = []
    creation_time = ""
    for raw_row in reader:
        row = {
            str(key): str(value or "").strip()
            for key, value in raw_row.items()
            if key is not None
        }
        symbol = row.get(symbol_field, "")
        if symbol.startswith("File Creation Time:"):
            creation_time = symbol.removeprefix("File Creation Time:").strip()
            continue
        if not symbol:
            continue
        rows.append(row)

    if not creation_time:
        raise ValueError("Nasdaq directory has no File Creation Time footer")
    if not rows:
        raise ValueError("Nasdaq directory contains no security rows")
    return ParsedDirectory(tuple(rows), creation_time)


def _yes_no(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text not in {"Y", "N"}:
        raise ValueError(f"invalid Nasdaq yes/no value: {value!r}")
    return text


def build_symbol_directory_snapshot(
    nasdaq_raw: bytes,
    other_raw: bytes,
) -> SymbolDirectorySnapshot:
    nasdaq = _parse_pipe_directory(
        nasdaq_raw,
        required_fields={
            "Symbol",
            "Security Name",
            "Market Category",
            "Test Issue",
            "Financial Status",
            "Round Lot Size",
            "ETF",
        },
        symbol_field="Symbol",
    )
    other = _parse_pipe_directory(
        other_raw,
        required_fields={
            "ACT Symbol",
            "Security Name",
            "Exchange",
            "CQS Symbol",
            "ETF",
            "Round Lot Size",
            "Test Issue",
            "NASDAQ Symbol",
        },
        symbol_field="ACT Symbol",
    )

    normalized: list[dict[str, str]] = []
    for row in nasdaq.rows:
        symbol = row["Symbol"].upper()
        normalized.append(
            {
                "symbol": symbol,
                "security_name": row["Security Name"],
                "source": "nasdaqlisted",
                "listing_exchange_code": "Q",
                "listing_exchange_name": "NASDAQ",
                "market_category": row["Market Category"].upper(),
                "test_issue": _yes_no(row["Test Issue"]),
                "financial_status": row["Financial Status"].upper(),
                "etf": _yes_no(row["ETF"]),
                "round_lot_size": row["Round Lot Size"],
                "cqs_symbol": "",
                "nasdaq_symbol": symbol,
            }
        )

    for row in other.rows:
        symbol = row["ACT Symbol"].upper()
        exchange_code = row["Exchange"].upper()
        normalized.append(
            {
                "symbol": symbol,
                "security_name": row["Security Name"],
                "source": "otherlisted",
                "listing_exchange_code": exchange_code,
                "listing_exchange_name": EXCHANGE_NAMES.get(
                    exchange_code, "UNKNOWN"
                ),
                "market_category": "",
                "test_issue": _yes_no(row["Test Issue"]),
                "financial_status": "",
                "etf": _yes_no(row["ETF"]),
                "round_lot_size": row["Round Lot Size"],
                "cqs_symbol": row["CQS Symbol"].upper(),
                "nasdaq_symbol": row["NASDAQ Symbol"].upper(),
            }
        )

    normalized.sort(key=lambda item: item["symbol"])
    symbols = [row["symbol"] for row in normalized]
    duplicates = sorted(
        symbol for symbol, count in Counter(symbols).items() if count > 1
    )
    if duplicates:
        raise ValueError(
            "Nasdaq directories contain duplicate normalized symbols: "
            + ", ".join(duplicates[:10])
        )

    return SymbolDirectorySnapshot(
        nasdaq_raw=nasdaq_raw,
        other_raw=other_raw,
        nasdaq_file_creation_time=nasdaq.file_creation_time,
        other_file_creation_time=other.file_creation_time,
        normalized_rows=tuple(normalized),
    )


def _write_csv(
    path: Path,
    rows: Iterable[Mapping[str, Any]],
) -> None:
    with path.open("x", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=NORMALIZED_FIELDS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def write_symbol_directory_artifacts(
    *,
    output_dir: str | Path,
    snapshot: SymbolDirectorySnapshot,
    retrieved_at_et: datetime,
    nasdaq_url: str = NASDAQ_LISTED_URL,
    other_url: str = OTHER_LISTED_URL,
) -> dict[str, Path]:
    if retrieved_at_et.tzinfo is None or retrieved_at_et.utcoffset() is None:
        raise ValueError("retrieved_at_et must be timezone-aware")

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=False)
    nasdaq_path = destination / "nasdaqlisted.txt"
    other_path = destination / "otherlisted.txt"
    normalized_path = destination / "normalized_all.csv"
    candidate_path = destination / "candidate_non_etf_non_test.csv"
    manifest_path = destination / "manifest.json"

    nasdaq_path.write_bytes(snapshot.nasdaq_raw)
    other_path.write_bytes(snapshot.other_raw)
    _write_csv(normalized_path, snapshot.normalized_rows)
    _write_csv(candidate_path, snapshot.candidate_rows)

    exchange_counts = Counter(
        row["listing_exchange_name"] for row in snapshot.normalized_rows
    )
    market_counts = Counter(
        row["market_category"]
        for row in snapshot.normalized_rows
        if row["market_category"]
    )
    financial_counts = Counter(
        row["financial_status"]
        for row in snapshot.normalized_rows
        if row["financial_status"]
    )
    manifest = {
        "directory_version": "nasdaq-symbol-directory-v1",
        "retrieved_at_et": retrieved_at_et.isoformat(),
        "retrieved_at_utc": retrieved_at_et.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "sources": {
            "nasdaqlisted": {
                "url": nasdaq_url,
                "file_creation_time": snapshot.nasdaq_file_creation_time,
            },
            "otherlisted": {
                "url": other_url,
                "file_creation_time": snapshot.other_file_creation_time,
            },
        },
        "counts": {
            "nasdaq_listed": sum(
                row["source"] == "nasdaqlisted"
                for row in snapshot.normalized_rows
            ),
            "other_listed": sum(
                row["source"] == "otherlisted"
                for row in snapshot.normalized_rows
            ),
            "combined": len(snapshot.normalized_rows),
            "test_issues": sum(
                row["test_issue"] == "Y" for row in snapshot.normalized_rows
            ),
            "etfs": sum(
                row["etf"] == "Y" for row in snapshot.normalized_rows
            ),
            "preliminary_candidates": len(snapshot.candidate_rows),
        },
        "listing_exchange_counts": dict(sorted(exchange_counts.items())),
        "market_category_counts": dict(sorted(market_counts.items())),
        "financial_status_counts": dict(sorted(financial_counts.items())),
        "artifacts": {},
    }
    for name, path in {
        "nasdaqlisted_raw": nasdaq_path,
        "otherlisted_raw": other_path,
        "normalized_all": normalized_path,
        "candidate_non_etf_non_test": candidate_path,
    }.items():
        manifest["artifacts"][name] = {
            "path": path.name,
            "sha256": sha256_file(path),
        }

    with manifest_path.open("x", encoding="utf-8") as file:
        json.dump(manifest, file, indent=2, sort_keys=True)
        file.write("\n")

    return {
        "nasdaq_raw": nasdaq_path,
        "other_raw": other_path,
        "normalized": normalized_path,
        "candidates": candidate_path,
        "manifest": manifest_path,
    }
