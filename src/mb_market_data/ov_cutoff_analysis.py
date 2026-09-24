"""Retrospective comparison of API Overnight Volume cutoff landmarks.

This module is deliberately separate from opening Focus production.  It
acquires one complete 00:00--09:30 ET candle stream, verifies that its 08:25
prefix reproduces immutable production evidence, and compares deterministic
top-N membership at later pre-open landmarks.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from mb_market_data.api_overnight_volume import (
    ET,
    APIOvernightVolumeBatch,
    APIOvernightVolumeCandle,
    sha256_file,
)
from mb_market_data.sampling_membership import SamplingHierarchyRevision


UTC = timezone.utc
OV_CUTOFF_ANALYSIS_VERSION = "api-ov-cutoff-analysis-v1"
ANALYSIS_WINDOW_END = time(9, 30)
CUTOFFS: tuple[tuple[str, time], ...] = (
    ("ov_0825", time(8, 25)),
    ("ov_0900", time(9, 0)),
    ("ov_0915", time(9, 15)),
    ("ov_0925", time(9, 25)),
    ("ov_final", time(9, 30)),
)
DELTAS: tuple[tuple[str, str, str], ...] = (
    ("ov_0825_to_0900", "ov_0825", "ov_0900"),
    ("ov_0900_to_0915", "ov_0900", "ov_0915"),
    ("ov_0915_to_0925", "ov_0915", "ov_0925"),
    ("ov_0925_to_0930", "ov_0925", "ov_final"),
    ("ov_near_open", "ov_0825", "ov_final"),
)


@dataclass(frozen=True, slots=True)
class ProductionOVBaseline:
    """Verified immutable 08:25 production evidence."""

    manifest_path: Path
    manifest_sha256: str
    observations_path: Path
    observations_sha256: str
    values: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class OVCutoffMetric:
    """One symbol's values and ranks at every cutoff."""

    symbol: str
    status: str
    candle_count: int
    baseline_ov_0825: int
    baseline_match: bool | None
    volumes: Mapping[str, int | None]
    deltas: Mapping[str, int | None]
    ranks: Mapping[str, int | None]
    selected: Mapping[str, bool]
    attempts: int
    request_started_at_utc: datetime
    response_received_at_utc: datetime | None
    http_status: int | None
    detail: str | None


@dataclass(frozen=True, slots=True)
class OVCutoffAnalysis:
    """Deterministic multi-cutoff comparison."""

    limit: int
    metrics: tuple[OVCutoffMetric, ...]
    membership: Mapping[str, tuple[str, ...]]
    transitions: tuple[Mapping[str, Any], ...]
    baseline_mismatch_symbols: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return (
            all(item.status == "ok" for item in self.metrics)
            and not self.baseline_mismatch_symbols
        )


@dataclass(frozen=True, slots=True)
class OVCutoffAnalysisArtifacts:
    """Paths written for one immutable cutoff analysis."""

    root: Path
    metrics: Path
    membership: Path
    candles: Path
    manifest: Path


def _read_json_object(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError(f"JSON must contain one object: {path}")
    return value


def _required_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonblank text")
    return value.strip()


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _utc_text(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def load_production_ov_baseline(
    manifest_path: str | Path,
    opening: SamplingHierarchyRevision,
) -> ProductionOVBaseline:
    """Load and cryptographically verify production 08:25 evidence."""

    path = Path(manifest_path).resolve()
    manifest = _read_json_object(path)
    if manifest.get("production_eligible") is not True:
        raise ValueError("production OV manifest is not production eligible")
    if manifest.get("complete_opening_uni") is not True:
        raise ValueError("production OV manifest is not complete opening Uni")
    if _required_text(manifest.get("session_date"), "session_date") != (
        opening.session_date.isoformat()
    ):
        raise ValueError("production OV session differs from opening proposal")
    window_end = datetime.fromisoformat(
        _required_text(manifest.get("window_end_et"), "window_end_et")
    )
    if (window_end.hour, window_end.minute) != (8, 25):
        raise ValueError("production OV baseline must end at 08:25 ET")
    if _nonnegative_int(
        manifest.get("opening_uni_count"), "opening_uni_count"
    ) != len(opening.uni_symbols):
        raise ValueError("production OV opening-Uni count differs")

    opening_info = _required_mapping(manifest.get("opening"), "opening")
    if _required_text(
        opening_info.get("content_sha256"), "opening content SHA-256"
    ) != opening.content_sha256:
        raise ValueError("production OV opening content SHA-256 differs")

    artifacts = _required_mapping(manifest.get("artifacts"), "artifacts")
    observation_info = _required_mapping(
        artifacts.get("api_ov_observations"), "api_ov_observations"
    )
    observation_path = path.parent / _required_text(
        observation_info.get("path"), "observation path"
    )
    expected_sha = _required_text(
        observation_info.get("sha256"), "observation SHA-256"
    )
    actual_sha = sha256_file(observation_path)
    if actual_sha != expected_sha:
        raise ValueError("production OV observation SHA-256 differs")

    values: dict[str, int] = {}
    with observation_path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"symbol", "status", "ov_decision"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError("production OV observations have invalid schema")
        for row in reader:
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol or symbol in values:
                raise ValueError("production OV observations have bad symbols")
            if str(row.get("status") or "").strip() != "ok":
                raise ValueError(f"production OV baseline is not OK for {symbol}")
            try:
                value = int(str(row.get("ov_decision") or ""))
            except ValueError as error:
                raise ValueError(
                    f"production OV value is invalid for {symbol}"
                ) from error
            values[symbol] = _nonnegative_int(value, f"OV value for {symbol}")

    expected_symbols = set(opening.uni_symbols)
    if set(values) != expected_symbols:
        raise ValueError("production OV symbols differ from opening Uni")
    return ProductionOVBaseline(
        manifest_path=path,
        manifest_sha256=sha256_file(path),
        observations_path=observation_path,
        observations_sha256=actual_sha,
        values=values,
    )


def calculate_cutoff_volumes(
    candles: Sequence[APIOvernightVolumeCandle],
    *,
    trade_date: date,
) -> dict[str, int]:
    """Calculate cumulative volume at every configured half-open cutoff."""

    result: dict[str, int] = {}
    for name, cutoff in CUTOFFS:
        boundary = datetime.combine(trade_date, cutoff, tzinfo=ET)
        result[name] = sum(
            candle.volume for candle in candles if candle.start_et < boundary
        )
    values = tuple(result.values())
    if values != tuple(sorted(values)):
        raise ValueError("cutoff volumes are not monotonic")
    return result


def analyze_cutoff_batch(
    batch: APIOvernightVolumeBatch,
    *,
    opening_symbols: Sequence[str],
    baseline: ProductionOVBaseline,
    limit: int,
) -> OVCutoffAnalysis:
    """Compare deterministic top-N membership at all cutoff landmarks."""

    if limit < 1:
        raise ValueError("limit must be positive")
    expected = tuple(str(symbol).strip().upper() for symbol in opening_symbols)
    observed = tuple(item.symbol for item in batch.observations)
    if observed != expected:
        raise ValueError("analysis acquisition order differs from opening Uni")
    if set(baseline.values) != set(expected):
        raise ValueError("baseline symbols differ from opening Uni")

    volumes_by_symbol: dict[str, dict[str, int | None]] = {}
    baseline_match: dict[str, bool | None] = {}
    for observation in batch.observations:
        if observation.usable:
            values: dict[str, int | None] = calculate_cutoff_volumes(
                observation.candles,
                trade_date=batch.trade_date,
            )
            baseline_match[observation.symbol] = (
                values["ov_0825"] == baseline.values[observation.symbol]
            )
        else:
            values = {name: None for name, _ in CUTOFFS}
            baseline_match[observation.symbol] = None
        volumes_by_symbol[observation.symbol] = values

    membership: dict[str, tuple[str, ...]] = {}
    ranks: dict[str, dict[str, int]] = {}
    for cutoff_name, _ in CUTOFFS:
        ranked = sorted(
            (
                (symbol, values[cutoff_name])
                for symbol, values in volumes_by_symbol.items()
                if values[cutoff_name] is not None
            ),
            key=lambda item: (-int(item[1]), item[0]),
        )
        ranks[cutoff_name] = {
            symbol: rank
            for rank, (symbol, _) in enumerate(ranked, start=1)
        }
        membership[cutoff_name] = tuple(
            symbol for symbol, _ in ranked[:limit]
        )

    transitions: list[Mapping[str, Any]] = []
    for (before_name, _), (after_name, _) in zip(CUTOFFS, CUTOFFS[1:]):
        before = membership[before_name]
        after = membership[after_name]
        before_set = set(before)
        after_set = set(after)
        transitions.append(
            {
                "from": before_name,
                "to": after_name,
                "overlap_count": len(before_set & after_set),
                "retained": [symbol for symbol in after if symbol in before_set],
                "entrants": [symbol for symbol in after if symbol not in before_set],
                "exits": [symbol for symbol in before if symbol not in after_set],
            }
        )

    observation_by_symbol = {
        item.symbol: item for item in batch.observations
    }
    metrics: list[OVCutoffMetric] = []
    for symbol in sorted(expected):
        observation = observation_by_symbol[symbol]
        values = volumes_by_symbol[symbol]
        deltas: dict[str, int | None] = {}
        for delta_name, start_name, end_name in DELTAS:
            start_value = values[start_name]
            end_value = values[end_name]
            deltas[delta_name] = (
                None
                if start_value is None or end_value is None
                else end_value - start_value
            )
        metrics.append(
            OVCutoffMetric(
                symbol=symbol,
                status=observation.status.value,
                candle_count=observation.candle_count,
                baseline_ov_0825=baseline.values[symbol],
                baseline_match=baseline_match[symbol],
                volumes=values,
                deltas=deltas,
                ranks={
                    name: ranks[name].get(symbol) for name, _ in CUTOFFS
                },
                selected={
                    name: symbol in membership[name] for name, _ in CUTOFFS
                },
                attempts=observation.attempts,
                request_started_at_utc=observation.request_started_at_utc,
                response_received_at_utc=observation.response_received_at_utc,
                http_status=observation.http_status,
                detail=observation.detail,
            )
        )

    mismatches = tuple(
        sorted(symbol for symbol, match in baseline_match.items() if match is False)
    )
    return OVCutoffAnalysis(
        limit=limit,
        metrics=tuple(metrics),
        membership=membership,
        transitions=tuple(transitions),
        baseline_mismatch_symbols=mismatches,
    )


def _candle_payload(candle: APIOvernightVolumeCandle) -> dict[str, Any]:
    return {
        "start_et": candle.start_et.isoformat(),
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
    }


def write_cutoff_analysis_artifacts(
    output_dir: str | Path,
    *,
    opening_proposal_path: str | Path,
    opening: SamplingHierarchyRevision,
    baseline: ProductionOVBaseline,
    batch: APIOvernightVolumeBatch,
    analysis: OVCutoffAnalysis,
) -> OVCutoffAnalysisArtifacts:
    """Write an immutable, hashed retrospective analysis bundle."""

    root = Path(output_dir)
    if root.exists():
        raise FileExistsError(f"output directory already exists: {root}")
    opening_path = Path(opening_proposal_path).resolve()
    if not opening_path.is_file():
        raise FileNotFoundError(opening_path)
    root.mkdir(parents=True)
    metrics_path = root / "cutoff_metrics.csv"
    membership_path = root / "cutoff_membership.json"
    candles_path = root / "cutoff_candles.jsonl"
    manifest_path = root / "manifest.json"

    metric_fields = [
        "symbol",
        "status",
        "candle_count",
        "baseline_ov_0825",
        "baseline_match",
        *[name for name, _ in CUTOFFS],
        *[name for name, _, _ in DELTAS],
        *[f"rank_{name}" for name, _ in CUTOFFS],
        *[f"selected_{name}" for name, _ in CUTOFFS],
        "attempts",
        "request_started_at_utc",
        "response_received_at_utc",
        "http_status",
        "detail",
    ]
    with metrics_path.open("x", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=metric_fields)
        writer.writeheader()
        for item in analysis.metrics:
            row: dict[str, Any] = {
                "symbol": item.symbol,
                "status": item.status,
                "candle_count": item.candle_count,
                "baseline_ov_0825": item.baseline_ov_0825,
                "baseline_match": (
                    "" if item.baseline_match is None else item.baseline_match
                ),
                "attempts": item.attempts,
                "request_started_at_utc": _utc_text(
                    item.request_started_at_utc
                ),
                "response_received_at_utc": _utc_text(
                    item.response_received_at_utc
                ),
                "http_status": (
                    "" if item.http_status is None else item.http_status
                ),
                "detail": item.detail or "",
            }
            row.update(
                {
                    name: "" if value is None else value
                    for name, value in item.volumes.items()
                }
            )
            row.update(
                {
                    name: "" if value is None else value
                    for name, value in item.deltas.items()
                }
            )
            row.update(
                {
                    f"rank_{name}": "" if value is None else value
                    for name, value in item.ranks.items()
                }
            )
            row.update(
                {
                    f"selected_{name}": value
                    for name, value in item.selected.items()
                }
            )
            writer.writerow(row)

    membership_payload = {
        "limit": analysis.limit,
        "cutoffs": {
            name: {
                "cutoff_et": cutoff.strftime("%H:%M"),
                "symbols": list(analysis.membership[name]),
            }
            for name, cutoff in CUTOFFS
        },
        "transitions": list(analysis.transitions),
    }
    with membership_path.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(membership_payload, output, indent=2, sort_keys=True)
        output.write("\n")

    observation_by_symbol = {
        item.symbol: item for item in batch.observations
    }
    with candles_path.open("x", encoding="utf-8", newline="\n") as output:
        for symbol in sorted(observation_by_symbol):
            item = observation_by_symbol[symbol]
            payload = {
                "symbol": symbol,
                "status": item.status.value,
                "candles": [_candle_payload(candle) for candle in item.candles],
            }
            output.write(json.dumps(payload, sort_keys=True) + "\n")

    status_counts = Counter(item.status for item in analysis.metrics)
    manifest = {
        "analysis_version": OV_CUTOFF_ANALYSIS_VERSION,
        "session_date": batch.trade_date.isoformat(),
        "window_start_et": batch.window_start_et.isoformat(),
        "window_end_et": batch.window_end_et.isoformat(),
        "frequency_minutes": 5,
        "started_at_utc": _utc_text(batch.started_at_utc),
        "completed_at_utc": _utc_text(batch.completed_at_utc),
        "request_interval_seconds": batch.request_interval_seconds,
        "request_count": batch.request_count,
        "requested_symbols": len(batch.observations),
        "successful_symbols": batch.successful_count,
        "failed_symbols": batch.failed_count,
        "status_counts": dict(sorted(status_counts.items())),
        "limit": analysis.limit,
        "cutoffs": {
            name: cutoff.strftime("%H:%M") for name, cutoff in CUTOFFS
        },
        "baseline_match_count": sum(
            item.baseline_match is True for item in analysis.metrics
        ),
        "baseline_mismatch_count": len(
            analysis.baseline_mismatch_symbols
        ),
        "baseline_mismatch_symbols": list(
            analysis.baseline_mismatch_symbols
        ),
        "analysis_complete": analysis.complete,
        "inputs": {
            "opening_proposal": {
                "path": str(opening_path),
                "sha256": sha256_file(opening_path),
                "content_sha256": opening.content_sha256,
            },
            "production_ov_manifest": {
                "path": str(baseline.manifest_path),
                "sha256": baseline.manifest_sha256,
            },
            "production_ov_observations": {
                "path": str(baseline.observations_path),
                "sha256": baseline.observations_sha256,
            },
        },
        "artifacts": {
            "cutoff_metrics": {
                "path": metrics_path.name,
                "sha256": sha256_file(metrics_path),
            },
            "cutoff_membership": {
                "path": membership_path.name,
                "sha256": sha256_file(membership_path),
            },
            "cutoff_candles": {
                "path": candles_path.name,
                "sha256": sha256_file(candles_path),
            },
        },
    }
    with manifest_path.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(manifest, output, indent=2, sort_keys=True)
        output.write("\n")

    return OVCutoffAnalysisArtifacts(
        root=root,
        metrics=metrics_path,
        membership=membership_path,
        candles=candles_path,
        manifest=manifest_path,
    )
