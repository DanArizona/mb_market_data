"""
Batched Schwab quote acquisition.

The Schwab /marketdata/v1/quotes endpoint accepts multiple symbols in a
single HTTP request.

Empirical capability probing on 2026-08-13 established:

    - Schwab explicitly rejects search combinations exceeding 500 items.
    - 400-symbol requests work successfully.
    - Initial production default: 400 symbols per request.

This module does not authenticate Schwab clients and does not perform
scheduling.  Callers supply an already-authenticated client.

Every normalized input symbol receives an explicit result.  A symbol is
never silently discarded.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


SCHWAB_QUOTE_MAX_SEARCH_ITEMS = 500
DEFAULT_QUOTE_BATCH_SIZE = 400


class QuoteStatus(str, Enum):
    """Result of attempting to acquire one symbol's quote."""

    QUOTE = "quote"
    INVALID = "invalid"
    MISSING = "missing"
    REQUEST_ERROR = "request_error"


@dataclass(frozen=True)
class QuoteResult:
    """Acquisition result for one requested symbol."""

    symbol: str
    status: QuoteStatus
    quote: Mapping[str, Any] | None
    detail: str | None
    batch_number: int

    @property
    def usable(self) -> bool:
        """True when Schwab returned quote data for the symbol."""

        return self.status == QuoteStatus.QUOTE


@dataclass(frozen=True)
class QuoteBatchResult:
    """Combined results of a batched Schwab quote acquisition."""

    results: tuple[QuoteResult, ...]
    request_count: int
    batch_size: int
    unexpected_symbols: tuple[str, ...]

    @property
    def usable_results(self) -> tuple[QuoteResult, ...]:
        return tuple(
            result
            for result in self.results
            if result.usable
        )

    @property
    def unavailable_results(self) -> tuple[QuoteResult, ...]:
        return tuple(
            result
            for result in self.results
            if not result.usable
        )

    def status_counts(self) -> Counter[QuoteStatus]:
        return Counter(
            result.status
            for result in self.results
        )

    def by_symbol(self) -> dict[str, QuoteResult]:
        return {
            result.symbol: result
            for result in self.results
        }


def normalize_symbols(
    symbols: Iterable[str] | str,
) -> tuple[str, ...]:
    """
    Normalize and deduplicate symbols while preserving input order.

    A comma-separated string is accepted for convenience.
    """

    if isinstance(symbols, str):
        source = symbols.split(",")
    else:
        source = symbols

    normalized: list[str] = []
    seen: set[str] = set()

    for raw_symbol in source:
        if not isinstance(raw_symbol, str):
            raise TypeError(
                "Every symbol must be a string, "
                f"got {type(raw_symbol).__name__}"
            )

        symbol = raw_symbol.strip().upper()

        if not symbol:
            continue

        if symbol in seen:
            continue

        seen.add(symbol)
        normalized.append(symbol)

    return tuple(normalized)


def _invalid_symbols(
    payload: Mapping[str, Any],
) -> set[str]:
    errors = payload.get("errors")

    if not isinstance(errors, Mapping):
        return set()

    invalid = errors.get("invalidSymbols")

    if not isinstance(invalid, list):
        return set()

    return {
        str(symbol).strip().upper()
        for symbol in invalid
        if str(symbol).strip()
    }


def _quote_entries(
    payload: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    entries: dict[str, Mapping[str, Any]] = {}

    for key, value in payload.items():
        if str(key).casefold() == "errors":
            continue

        symbol = str(key).strip().upper()

        if not symbol:
            continue

        if isinstance(value, Mapping):
            entries[symbol] = value

    return entries


def _http_error_detail(
    response: Any,
    payload: Any,
) -> str:
    status_code = getattr(
        response,
        "status_code",
        "unknown",
    )

    detail = f"HTTP {status_code}"

    if isinstance(payload, Mapping):
        errors = payload.get("errors")

        if isinstance(errors, list):
            messages: list[str] = []

            for item in errors:
                if not isinstance(item, Mapping):
                    continue

                message = item.get("detail")

                if message:
                    messages.append(str(message))

            if messages:
                detail += ": " + "; ".join(messages)

    return detail


def fetch_quotes_batched(
    client: Any,
    symbols: Iterable[str] | str,
    *,
    fields: str = "quote",
    batch_size: int = DEFAULT_QUOTE_BATCH_SIZE,
) -> QuoteBatchResult:
    """
    Fetch Schwab quotes using multiple-symbol requests.

    Parameters
    ----------
    client
        Already-authenticated Schwab client having a quotes() method.

    symbols
        Symbols to retrieve.  Symbols are uppercased and duplicates are
        removed while preserving order.

    fields
        Value passed to Schwab's quotes(fields=...) parameter.

    batch_size
        Number of symbols in each HTTP request.  Must be between 1 and
        Schwab's demonstrated maximum of 500.

    Returns
    -------
    QuoteBatchResult
        Contains one explicit QuoteResult for every normalized symbol.

    Notes
    -----
    An HTTP failure affects only the symbols in that particular batch.
    Processing continues with later batches.
    """

    if isinstance(batch_size, bool) or not isinstance(
        batch_size,
        int,
    ):
        raise TypeError(
            "batch_size must be an integer"
        )

    if not (
        1
        <= batch_size
        <= SCHWAB_QUOTE_MAX_SEARCH_ITEMS
    ):
        raise ValueError(
            "batch_size must be between 1 and "
            f"{SCHWAB_QUOTE_MAX_SEARCH_ITEMS}"
        )

    normalized = normalize_symbols(
        symbols
    )

    if not normalized:
        return QuoteBatchResult(
            results=(),
            request_count=0,
            batch_size=batch_size,
            unexpected_symbols=(),
        )

    results: list[QuoteResult] = []
    unexpected_symbols: set[str] = set()
    request_count = 0

    for start in range(
        0,
        len(normalized),
        batch_size,
    ):
        batch_number = (
            start // batch_size
        ) + 1

        batch = normalized[
            start : start + batch_size
        ]

        expected = set(batch)

        request_count += 1

        try:
            response = client.quotes(
                list(batch),
                fields=fields,
            )

        except Exception as exc:
            detail = (
                f"{type(exc).__name__}: {exc}"
            )

            for symbol in batch:
                results.append(
                    QuoteResult(
                        symbol=symbol,
                        status=QuoteStatus.REQUEST_ERROR,
                        quote=None,
                        detail=detail,
                        batch_number=batch_number,
                    )
                )

            continue

        try:
            payload = response.json()
        except Exception as exc:
            detail = (
                "Unreadable Schwab JSON response: "
                f"{type(exc).__name__}: {exc}"
            )

            for symbol in batch:
                results.append(
                    QuoteResult(
                        symbol=symbol,
                        status=QuoteStatus.REQUEST_ERROR,
                        quote=None,
                        detail=detail,
                        batch_number=batch_number,
                    )
                )

            continue

        if not getattr(
            response,
            "ok",
            False,
        ):
            detail = _http_error_detail(
                response,
                payload,
            )

            for symbol in batch:
                results.append(
                    QuoteResult(
                        symbol=symbol,
                        status=QuoteStatus.REQUEST_ERROR,
                        quote=None,
                        detail=detail,
                        batch_number=batch_number,
                    )
                )

            continue

        if not isinstance(
            payload,
            Mapping,
        ):
            detail = (
                "Schwab quotes JSON response "
                "is not an object."
            )

            for symbol in batch:
                results.append(
                    QuoteResult(
                        symbol=symbol,
                        status=QuoteStatus.REQUEST_ERROR,
                        quote=None,
                        detail=detail,
                        batch_number=batch_number,
                    )
                )

            continue

        quotes = _quote_entries(
            payload
        )

        invalid = _invalid_symbols(
            payload
        )

        unexpected_symbols.update(
            set(quotes)
            - expected
        )

        for symbol in batch:
            if symbol in quotes:
                results.append(
                    QuoteResult(
                        symbol=symbol,
                        status=QuoteStatus.QUOTE,
                        quote=quotes[symbol],
                        detail=None,
                        batch_number=batch_number,
                    )
                )

            elif symbol in invalid:
                results.append(
                    QuoteResult(
                        symbol=symbol,
                        status=QuoteStatus.INVALID,
                        quote=None,
                        detail=(
                            "Schwab reported symbol "
                            "as invalid."
                        ),
                        batch_number=batch_number,
                    )
                )

            else:
                results.append(
                    QuoteResult(
                        symbol=symbol,
                        status=QuoteStatus.MISSING,
                        quote=None,
                        detail=(
                            "Schwab returned neither "
                            "quote data nor an invalid-symbol "
                            "classification."
                        ),
                        batch_number=batch_number,
                    )
                )

    return QuoteBatchResult(
        results=tuple(results),
        request_count=request_count,
        batch_size=batch_size,
        unexpected_symbols=tuple(
            sorted(unexpected_symbols)
        ),
    )
