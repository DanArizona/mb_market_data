"""Publish one complete schema-v2 sampling hierarchy proposal."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from mb_market_data.quote_observation_store import (
    HIERARCHY_SCHEMA_VERSION,
    QuoteObservationStore,
    RecordResult,
)
from mb_market_data.sampling_membership import SamplingHierarchyRevision


PROPOSAL_FIELDS = frozenset(
    {
        "session_date",
        "revision",
        "effective_at",
        "uni_symbols",
        "focus_symbols",
        "hot_symbols",
        "source",
        "reason",
        "metadata",
    }
)
REQUIRED_PROPOSAL_FIELDS = frozenset(
    {
        "session_date",
        "revision",
        "effective_at",
        "uni_symbols",
        "focus_symbols",
        "hot_symbols",
        "source",
    }
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Atomically publish one complete Uni/Focus/Hot hierarchy "
            "revision to a schema-v2 daily quote journal."
        )
    )
    parser.add_argument("database", type=Path, help="Daily .sqlite3 file")
    parser.add_argument(
        "proposal",
        type=Path,
        help="JSON file containing the complete hierarchy proposal",
    )
    return parser.parse_args()


def load_proposal(path: str | Path) -> SamplingHierarchyRevision:
    """Load and validate one strict JSON hierarchy proposal."""

    proposal_path = Path(path)
    with proposal_path.open("r", encoding="utf-8-sig") as handle:
        payload: Any = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("proposal JSON must contain one object")

    unexpected = set(payload) - PROPOSAL_FIELDS
    missing = REQUIRED_PROPOSAL_FIELDS - set(payload)
    if unexpected:
        raise ValueError(
            "proposal contains unexpected fields: "
            + ", ".join(sorted(unexpected))
        )
    if missing:
        raise ValueError(
            "proposal is missing required fields: "
            + ", ".join(sorted(missing))
        )

    effective_at = datetime.fromisoformat(payload["effective_at"])
    if effective_at.tzinfo is None or effective_at.utcoffset() is None:
        raise ValueError("effective_at must include an explicit UTC offset")

    return SamplingHierarchyRevision(
        session_date=date.fromisoformat(payload["session_date"]),
        revision=payload["revision"],
        effective_at=effective_at,
        uni_symbols=payload["uni_symbols"],
        focus_symbols=payload["focus_symbols"],
        hot_symbols=payload["hot_symbols"],
        source=payload["source"],
        reason=payload.get("reason"),
        metadata=payload.get("metadata", {}),
    )


def publish_proposal(
    database_path: str | Path,
    proposal: SamplingHierarchyRevision,
) -> tuple[RecordResult, SamplingHierarchyRevision]:
    """Initialize the explicit v2 store, publish, and read back the result."""

    store = QuoteObservationStore(
        database_path,
        session_date=proposal.session_date,
        schema_version=HIERARCHY_SCHEMA_VERSION,
    )
    store.initialize()
    result = store.record_membership_revision(proposal)
    stored = {
        item.revision: item
        for item in store.membership_revisions_in_effective_order()
    }[proposal.revision]
    if stored.content_sha256 != proposal.content_sha256:
        raise RuntimeError(
            "published hierarchy content hash differs from the proposal"
        )
    return result, stored


def main() -> int:
    args = parse_args()
    try:
        proposal = load_proposal(args.proposal)
        result, stored = publish_proposal(args.database, proposal)
    except Exception as exc:
        print(f"Hierarchy publication ERROR: {type(exc).__name__}: {exc}")
        return 2

    print()
    print("Sampling hierarchy publication")
    print("=" * 79)
    print(f"Database         : {Path(args.database)}")
    print(f"Session date     : {stored.session_date.isoformat()}")
    print(f"Revision         : r{stored.revision}")
    print(f"Effective at     : {stored.effective_at.isoformat()}")
    print(f"Published at     : {stored.published_at.isoformat()}")
    print(f"Uni symbols      : {len(stored.uni_symbols):,}")
    print(f"Focus symbols    : {len(stored.focus_symbols):,}")
    print(f"Hot symbols      : {len(stored.hot_symbols):,}")
    print(f"Content SHA-256  : {stored.content_sha256}")
    print(f"Result           : {result.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
