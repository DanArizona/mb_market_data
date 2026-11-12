"""Atomic Uni/Focus/Hot sampling-membership contract.

The Watchlist Coordinator will eventually publish complete hierarchy
revisions.  This module defines that value independently of SQLite, polling,
and replay so those layers share one normalization and validation contract.

Membership is set-like: input order is not meaningful.  Symbols are therefore
normalized, deduplicated, and sorted before hierarchy validation or hashing.
The canonical hierarchy is always::

    Hot ⊆ Focus ⊆ Uni
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from mb_market_data.schwab_quotes import normalize_symbols


SAMPLING_HIERARCHY_CONTRACT = "nested-uni-focus-hot-v1"


class SamplingChannel(StrEnum):
    """Named channels in one hierarchical sampling revision."""

    UNI = "uni"
    FOCUS = "focus"
    HOT = "hot"


class MembershipRevisionError(ValueError):
    """A hierarchy revision or revision transition is invalid."""


class MembershipRevisionConflictError(MembershipRevisionError):
    """A revision number already identifies different canonical content."""


class RevisionTransition(StrEnum):
    """Valid relationship between stored state and a candidate revision."""

    INITIAL = "initial"
    NEXT = "next"
    ALREADY_PRESENT = "already_present"


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _require_nonblank(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")
    return value.strip()


def _utc_text(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _canonical_symbols(value: Any) -> tuple[str, ...]:
    return tuple(sorted(normalize_symbols(value)))


def _plain_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("metadata keys must be strings")
        return {
            key: _plain_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_plain_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("metadata numbers must be finite")
        return value
    raise TypeError(
        f"unsupported metadata value: {type(value).__name__}"
    )


def _canonical_metadata_json(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        raise TypeError("metadata must be a mapping")

    try:
        plain_value = _plain_json_value(value)
        return json.dumps(
            plain_value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "metadata must contain finite JSON-compatible values"
        ) from exc


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class SamplingHierarchyRevision:
    """One complete, immutable Uni/Focus/Hot membership snapshot.

    ``published_at`` is absent on a publication proposal and is assigned by
    the store when the revision commits.  It is intentionally excluded from
    ``content_sha256`` so an ambiguous commit can be retried idempotently.
    """

    session_date: date
    revision: int
    effective_at: datetime
    uni_symbols: tuple[str, ...]
    focus_symbols: tuple[str, ...]
    hot_symbols: tuple[str, ...]
    source: str
    reason: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    published_at: datetime | None = None
    contract: str = SAMPLING_HIERARCHY_CONTRACT
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if isinstance(self.session_date, datetime) or not isinstance(
            self.session_date, date
        ):
            raise TypeError("session_date must be a date")

        if isinstance(self.revision, bool) or not isinstance(
            self.revision, int
        ):
            raise TypeError("revision must be an integer")
        if self.revision < 0:
            raise ValueError("revision must be nonnegative")

        _require_aware(self.effective_at, "effective_at")
        if self.published_at is not None:
            _require_aware(self.published_at, "published_at")
            if self.effective_at < self.published_at:
                raise MembershipRevisionError(
                    "effective_at cannot precede published_at"
                )

        source = _require_nonblank(self.source, "source")
        reason = self.reason
        if reason is not None:
            if not isinstance(reason, str):
                raise TypeError("reason must be a string or None")
            reason = reason.strip() or None

        if self.contract != SAMPLING_HIERARCHY_CONTRACT:
            raise MembershipRevisionError(
                "unsupported sampling hierarchy contract: "
                f"{self.contract!r}"
            )

        uni_symbols = _canonical_symbols(self.uni_symbols)
        focus_symbols = _canonical_symbols(self.focus_symbols)
        hot_symbols = _canonical_symbols(self.hot_symbols)

        if not uni_symbols:
            raise MembershipRevisionError(
                "uni_symbols must contain at least one symbol"
            )

        uni_members = set(uni_symbols)
        focus_members = set(focus_symbols)
        hot_members = set(hot_symbols)
        focus_outside_uni = tuple(sorted(focus_members - uni_members))
        hot_outside_focus = tuple(sorted(hot_members - focus_members))

        if focus_outside_uni:
            raise MembershipRevisionError(
                "Focus must be a subset of Uni; outside Uni: "
                f"{', '.join(focus_outside_uni)}"
            )
        if hot_outside_focus:
            raise MembershipRevisionError(
                "Hot must be a subset of Focus; outside Focus: "
                f"{', '.join(hot_outside_focus)}"
            )

        metadata_json = _canonical_metadata_json(self.metadata)
        metadata_value = json.loads(metadata_json)
        frozen_metadata = _freeze_json(metadata_value)

        object.__setattr__(self, "source", source)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "uni_symbols", uni_symbols)
        object.__setattr__(self, "focus_symbols", focus_symbols)
        object.__setattr__(self, "hot_symbols", hot_symbols)
        object.__setattr__(self, "metadata", frozen_metadata)

        payload = {
            "contract": self.contract,
            "session_date": self.session_date.isoformat(),
            "revision": self.revision,
            "effective_at_utc": _utc_text(self.effective_at),
            "uni_symbols": uni_symbols,
            "focus_symbols": focus_symbols,
            "hot_symbols": hot_symbols,
            "source": source,
            "reason": reason,
            "metadata": metadata_value,
        }
        canonical_payload = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        object.__setattr__(
            self,
            "content_sha256",
            hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest(),
        )

    def symbols_for(
        self,
        channel: SamplingChannel | str,
    ) -> tuple[str, ...]:
        """Return the canonical membership for one hierarchy channel."""

        try:
            normalized = SamplingChannel(str(channel).strip().casefold())
        except ValueError as exc:
            raise ValueError(f"unknown sampling channel: {channel!r}") from exc

        if normalized is SamplingChannel.UNI:
            return self.uni_symbols
        if normalized is SamplingChannel.FOCUS:
            return self.focus_symbols
        return self.hot_symbols


def assess_revision_candidate(
    current: SamplingHierarchyRevision | None,
    candidate: SamplingHierarchyRevision,
) -> RevisionTransition:
    """Validate and classify a proposed revision against current state.

    This pure transition check will be reused by the schema-v2 store.  It does
    not publish anything and therefore cannot assign ``published_at``.
    """

    if not isinstance(candidate, SamplingHierarchyRevision):
        raise TypeError("candidate must be a SamplingHierarchyRevision")

    if current is None:
        if candidate.revision != 0:
            raise MembershipRevisionError(
                "the initial hierarchy revision must be r0"
            )
        return RevisionTransition.INITIAL

    if not isinstance(current, SamplingHierarchyRevision):
        raise TypeError("current must be a SamplingHierarchyRevision or None")
    if candidate.session_date != current.session_date:
        raise MembershipRevisionError(
            "current and candidate session dates do not match"
        )

    if candidate.revision == current.revision:
        if candidate.content_sha256 == current.content_sha256:
            return RevisionTransition.ALREADY_PRESENT
        raise MembershipRevisionConflictError(
            f"hierarchy r{candidate.revision} already has different content"
        )

    if candidate.revision < current.revision:
        raise MembershipRevisionError(
            f"stale hierarchy revision r{candidate.revision}; "
            f"current is r{current.revision}"
        )
    if candidate.revision != current.revision + 1:
        raise MembershipRevisionError(
            f"hierarchy revision gap: expected r{current.revision + 1}, "
            f"got r{candidate.revision}"
        )
    if candidate.effective_at < current.effective_at:
        raise MembershipRevisionError(
            "candidate effective_at cannot precede current effective_at"
        )
    if (
        current.published_at is not None
        and candidate.published_at is not None
        and candidate.published_at < current.published_at
    ):
        raise MembershipRevisionError(
            "candidate published_at cannot precede current published_at"
        )
    if (
        candidate.effective_at == current.effective_at
        and candidate.uni_symbols == current.uni_symbols
        and candidate.focus_symbols == current.focus_symbols
        and candidate.hot_symbols == current.hot_symbols
        and candidate.source == current.source
        and candidate.reason == current.reason
        and candidate.metadata == current.metadata
        and candidate.contract == current.contract
    ):
        raise MembershipRevisionError(
            "candidate does not change membership or retained provenance"
        )

    return RevisionTransition.NEXT
