"""
Read and validate ThinkOrSwim Watchlist CSV exports.

ThinkOrSwim Watchlist exports contain several preamble lines before the
actual CSV header.  This module locates the header dynamically rather
than assuming a fixed number of preamble lines.

The initial production use is ingestion of the OV_DECISION custom quote.

A valid numeric zero is intentionally distinguished from unavailable or
invalid values such as:

    loading
    NaN
    blank
    custom expression subscription limit exceeded
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path


class OVDecisionStatus(str, Enum):
    """Classification of a ThinkOrSwim OV_DECISION value."""

    NUMERIC = "numeric"
    ZERO = "zero"
    LOADING = "loading"
    NAN = "nan"
    BLANK = "blank"
    SUBSCRIPTION_LIMIT = "subscription_limit"
    INVALID = "invalid"


USABLE_OV_STATUSES = {
    OVDecisionStatus.NUMERIC,
    OVDecisionStatus.ZERO,
}


@dataclass(frozen=True)
class TosWatchlistRow:
    """One symbol row from a ThinkOrSwim Watchlist export."""

    symbol: str
    ov_decision: Decimal | None
    ov_decision_status: OVDecisionStatus
    raw_ov_decision: str
    fields: dict[str, str]

    @property
    def usable_ov_decision(self) -> bool:
        """True when OV_DECISION is a valid nonnegative numeric value."""

        return self.ov_decision_status in USABLE_OV_STATUSES


@dataclass(frozen=True)
class TosWatchlist:
    """Parsed ThinkOrSwim Watchlist export."""

    path: Path
    headers: tuple[str, ...]
    header_line_number: int
    rows: tuple[TosWatchlistRow, ...]

    @property
    def usable_rows(self) -> tuple[TosWatchlistRow, ...]:
        """Rows having a usable OV_DECISION value."""

        return tuple(
            row
            for row in self.rows
            if row.usable_ov_decision
        )

    @property
    def unavailable_rows(self) -> tuple[TosWatchlistRow, ...]:
        """Rows whose OV_DECISION cannot currently be used."""

        return tuple(
            row
            for row in self.rows
            if not row.usable_ov_decision
        )

    def status_counts(self) -> Counter[OVDecisionStatus]:
        """Count rows by OV_DECISION classification."""

        return Counter(
            row.ov_decision_status
            for row in self.rows
        )


def classify_ov_decision(
    raw_value: str,
) -> tuple[Decimal | None, OVDecisionStatus]:
    """
    Parse and classify one OV_DECISION value.

    Finite nonnegative numeric representations such as:

        607650
        607650.0
        12.5
        2.799640564802E8
        "607,650"

    are accepted and returned as Decimal values.

    OV_DECISION is a ThinkOrSwim-derived decision value and is not
    required to be an integral share count.
    """

    text = raw_value.strip()

    if not text or text.casefold() == "<empty>":
        return None, OVDecisionStatus.BLANK

    normalized = text.casefold()

    if normalized == "loading":
        return None, OVDecisionStatus.LOADING

    if normalized == "nan":
        return None, OVDecisionStatus.NAN

    if (
        "custom expression subscription limit exceeded"
        in normalized
    ):
        return None, OVDecisionStatus.SUBSCRIPTION_LIMIT

    numeric_text = text.replace(",", "")

    try:
        value = Decimal(numeric_text)
    except InvalidOperation:
        return None, OVDecisionStatus.INVALID

    if not value.is_finite():
        if value.is_nan():
            return None, OVDecisionStatus.NAN

        return None, OVDecisionStatus.INVALID

    if value < 0:
        return None, OVDecisionStatus.INVALID

    if value == 0:
        return Decimal(0), OVDecisionStatus.ZERO

    return value, OVDecisionStatus.NUMERIC


def read_tos_watchlist(
    path: str | Path,
) -> TosWatchlist:
    """
    Read a ThinkOrSwim Watchlist CSV containing OV_DECISION.

    The CSV header is located by finding a row whose first field is
    exactly ``Symbol`` after whitespace is removed.

    Raises
    ------
    FileNotFoundError
        If the file does not exist.

    ValueError
        If the Symbol header cannot be found or OV_DECISION is absent.
    """

    csv_path = Path(path)

    if not csv_path.is_file():
        raise FileNotFoundError(csv_path)

    with csv_path.open(
        "r",
        newline="",
        encoding="utf-8-sig",
    ) as file:
        all_rows = list(csv.reader(file))

    header_index: int | None = None

    for index, values in enumerate(all_rows):
        if (
            values
            and values[0].strip() == "Symbol"
        ):
            header_index = index
            break

    if header_index is None:
        raise ValueError(
            f"No Symbol header found in {csv_path}"
        )

    headers = tuple(
        value.strip()
        for value in all_rows[header_index]
    )

    if "OV_DECISION" not in headers:
        raise ValueError(
            "ThinkOrSwim Watchlist CSV does not contain "
            "required column 'OV_DECISION'."
        )

    parsed_rows: list[TosWatchlistRow] = []

    for values in all_rows[header_index + 1 :]:
        if not values:
            continue

        if len(values) < len(headers):
            values = values + (
                [""] * (len(headers) - len(values))
            )

        fields = {
            header: value
            for header, value in zip(
                headers,
                values,
            )
        }

        symbol = fields.get(
            "Symbol",
            "",
        ).strip().upper()

        if not symbol:
            continue

        raw_ov_decision = fields.get(
            "OV_DECISION",
            "",
        )

        (
            ov_decision,
            status,
        ) = classify_ov_decision(
            raw_ov_decision
        )

        parsed_rows.append(
            TosWatchlistRow(
                symbol=symbol,
                ov_decision=ov_decision,
                ov_decision_status=status,
                raw_ov_decision=raw_ov_decision,
                fields=fields,
            )
        )

    return TosWatchlist(
        path=csv_path.resolve(),
        headers=headers,
        header_line_number=header_index + 1,
        rows=tuple(parsed_rows),
    )