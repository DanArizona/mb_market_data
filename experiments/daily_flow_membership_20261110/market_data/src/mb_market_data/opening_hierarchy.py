"""Build schema-v2 opening membership from a daily universe artifact."""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mb_market_data.daily_universe import sha256_file
from mb_market_data.sampling_membership import SamplingHierarchyRevision


ET = ZoneInfo("America/New_York")
OPENING_HIERARCHY_SOURCE = "daily-universe-production-v1"
OPENING_PROPOSAL_FIELDS = frozenset(
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


def _read_json_object(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise ValueError(f"JSON must contain one object: {path}")
    return value


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"universe manifest has no {name}")
    return value.strip()


def _artifact_sha(manifest: Mapping[str, Any], name: str) -> str:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ValueError("universe manifest has no artifacts object")
    artifact = artifacts.get(name)
    if not isinstance(artifact, Mapping):
        raise ValueError(f"universe manifest has no {name} artifact")
    return _required_text(artifact.get("sha256"), f"{name} sha256")


def _verify_artifact(
    path: Path,
    manifest: Mapping[str, Any],
    name: str,
) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"missing universe artifact: {path}")
    actual = sha256_file(path)
    expected = _artifact_sha(manifest, name)
    if actual != expected:
        raise ValueError(
            f"{name} SHA-256 differs from the universe manifest"
        )
    return actual


def _read_symbols(path: Path) -> tuple[str, ...]:
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None or "symbol" not in reader.fieldnames:
            raise ValueError(f"symbol CSV has no 'symbol' column: {path}")
        symbols = tuple(
            str(row.get("symbol") or "").strip().upper() for row in reader
        )
    if not symbols or any(not symbol for symbol in symbols):
        raise ValueError("opening Uni must contain nonblank symbols")
    if len(symbols) != len(set(symbols)):
        raise ValueError("opening Uni contains duplicate symbols")
    return symbols


def build_opening_hierarchy(
    universe_dir: str | Path,
) -> SamplingHierarchyRevision:
    """Build deterministic r0 with Uni populated and Focus/Hot empty."""

    directory = Path(universe_dir)
    manifest_path = directory / "manifest.json"
    symbols_path = directory / "uni_symbols.csv"
    ledger_path = directory / "decision_ledger.csv"
    manifest = _read_json_object(manifest_path)

    source_session = date.fromisoformat(
        _required_text(manifest.get("session_date"), "session_date")
    )
    target_session = date.fromisoformat(
        _required_text(manifest.get("target_date"), "target_date")
    )
    if target_session <= source_session:
        raise ValueError("universe target_date must follow session_date")

    symbols_sha = _verify_artifact(symbols_path, manifest, "uni_symbols")
    ledger_sha = _verify_artifact(
        ledger_path,
        manifest,
        "decision_ledger",
    )
    symbols = _read_symbols(symbols_path)
    counts = manifest.get("counts")
    if not isinstance(counts, Mapping):
        raise ValueError("universe manifest has no counts object")
    included = counts.get("included")
    if isinstance(included, bool) or not isinstance(included, int):
        raise ValueError("universe manifest included count is invalid")
    if included != len(symbols):
        raise ValueError(
            "uni_symbols row count differs from universe manifest"
        )

    effective_at = datetime.combine(
        target_session,
        time(9, 30),
        tzinfo=ET,
    )
    metadata = {
        "universe_source_session_date": source_session.isoformat(),
        "universe_target_date": target_session.isoformat(),
        "selector_version": _required_text(
            manifest.get("selector_version"), "selector_version"
        ),
        "universe_manifest_sha256": sha256_file(manifest_path),
        "uni_symbols_sha256": symbols_sha,
        "decision_ledger_sha256": ledger_sha,
        "opening_focus_policy": "empty-awaiting-ov-base-set",
        "opening_hot_policy": "empty-awaiting-focus",
    }
    return SamplingHierarchyRevision(
        session_date=target_session,
        revision=0,
        effective_at=effective_at,
        uni_symbols=symbols,
        focus_symbols=(),
        hot_symbols=(),
        source=OPENING_HIERARCHY_SOURCE,
        reason="opening Uni from completed-session daily selector",
        metadata=metadata,
    )


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def opening_proposal_payload(
    revision: SamplingHierarchyRevision,
) -> dict[str, Any]:
    if revision.revision != 0:
        raise ValueError("opening hierarchy proposal must be r0")
    return {
        "session_date": revision.session_date.isoformat(),
        "revision": revision.revision,
        "effective_at": revision.effective_at.isoformat(),
        "uni_symbols": list(revision.uni_symbols),
        "focus_symbols": list(revision.focus_symbols),
        "hot_symbols": list(revision.hot_symbols),
        "source": revision.source,
        "reason": revision.reason,
        "metadata": _plain_json(revision.metadata),
    }


def load_opening_proposal(
    path: str | Path,
) -> SamplingHierarchyRevision:
    """Load and validate one exact opening-r0 proposal artifact."""

    proposal_path = Path(path)
    payload = _read_json_object(proposal_path)
    fields = set(payload)
    missing = sorted(OPENING_PROPOSAL_FIELDS - fields)
    unexpected = sorted(fields - OPENING_PROPOSAL_FIELDS)
    if missing or unexpected:
        problems = []
        if missing:
            problems.append(f"missing fields: {', '.join(missing)}")
        if unexpected:
            problems.append(f"unexpected fields: {', '.join(unexpected)}")
        raise ValueError(
            "invalid opening proposal schema (" + "; ".join(problems) + ")"
        )

    for name in ("uni_symbols", "focus_symbols", "hot_symbols"):
        value = payload[name]
        if not isinstance(value, list) or any(
            not isinstance(symbol, str) for symbol in value
        ):
            raise ValueError(f"opening proposal {name} must be a string list")
    if not isinstance(payload["metadata"], Mapping):
        raise ValueError("opening proposal metadata must be an object")
    if payload["reason"] is not None and not isinstance(
        payload["reason"], str
    ):
        raise ValueError("opening proposal reason must be a string or null")

    revision = SamplingHierarchyRevision(
        session_date=date.fromisoformat(
            _required_text(payload["session_date"], "session_date")
        ),
        revision=payload["revision"],
        effective_at=datetime.fromisoformat(
            _required_text(payload["effective_at"], "effective_at")
        ),
        uni_symbols=payload["uni_symbols"],
        focus_symbols=payload["focus_symbols"],
        hot_symbols=payload["hot_symbols"],
        source=payload["source"],
        reason=payload["reason"],
        metadata=payload["metadata"],
    )
    if revision.revision != 0:
        raise ValueError("opening hierarchy proposal must be r0")
    if revision.focus_symbols or revision.hot_symbols:
        raise ValueError("opening r0 must have empty Focus and Hot rosters")
    return revision


def write_opening_proposal(
    path: str | Path,
    revision: SamplingHierarchyRevision,
) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as file:
        json.dump(
            opening_proposal_payload(revision),
            file,
            indent=2,
            sort_keys=True,
        )
        file.write("\n")
    return output
