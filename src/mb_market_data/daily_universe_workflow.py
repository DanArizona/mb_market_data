"""Orchestration primitives for the post-close daily universe workflow."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Sequence

from mb_market_data.daily_universe import UniverseFilterConfig, sha256_file


WORKFLOW_VERSION = "daily-universe-production-v2"


@dataclass(frozen=True)
class WorkflowPaths:
    root: Path
    symbol_directory: Path
    market_snapshot: Path
    universe: Path
    opening_hierarchy: Path
    manifest: Path


@dataclass(frozen=True)
class WorkflowStage:
    name: str
    command: tuple[str, ...]


def workflow_paths(root: str | Path) -> WorkflowPaths:
    path = Path(root)
    return WorkflowPaths(
        root=path,
        symbol_directory=path / "symbol_directory",
        market_snapshot=path / "market_snapshot",
        universe=path / "universe",
        opening_hierarchy=path / "opening_hierarchy_r0.json",
        manifest=path / "workflow_manifest.json",
    )


def validate_workflow_dates(session_date: date, target_date: date) -> None:
    if target_date <= session_date:
        raise ValueError("target_date must be after session_date")


def build_stage_commands(
    *,
    repository_root: str | Path,
    python_executable: str,
    paths: WorkflowPaths,
    session_date: date,
    target_date: date,
    batch_size: int = 400,
    nasdaq_timeout: float = 30.0,
    schwab_timeout: int = 30,
    ecfg: str | None = None,
    minimum_volume: str = "10000",
    minimum_close: str = "0.10",
    minimum_market_cap: str = "4000000",
    maximum_market_cap: str = "40000000",
    publish_database: str | Path | None = None,
) -> tuple[WorkflowStage, ...]:
    validate_workflow_dates(session_date, target_date)
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    try:
        filter_config = UniverseFilterConfig(
            minimum_volume=Decimal(minimum_volume.replace(",", "")),
            minimum_close=Decimal(minimum_close.replace(",", "")),
            minimum_market_cap=Decimal(
                minimum_market_cap.replace(",", "")
            ),
            maximum_market_cap=Decimal(
                maximum_market_cap.replace(",", "")
            ),
        )
    except InvalidOperation as error:
        raise ValueError(
            "universe filter values must be decimal numbers"
        ) from error
    if not all(
        value.is_finite()
        for value in (
            filter_config.minimum_volume,
            filter_config.minimum_close,
            filter_config.minimum_market_cap,
            filter_config.maximum_market_cap,
        )
    ):
        raise ValueError("universe filter values must be finite")
    root = Path(repository_root)
    probes = root / "probes"

    directory_command = (
        python_executable,
        str(probes / "probe_nasdaq_symbol_directory.py"),
        "--timeout",
        str(nasdaq_timeout),
        "--output-dir",
        str(paths.symbol_directory),
    )
    snapshot_values = [
        python_executable,
        str(probes / "acquire_daily_universe_snapshot.py"),
        "--candidate-csv",
        str(paths.symbol_directory / "candidate_non_etf_non_test.csv"),
        "--session-date",
        session_date.isoformat(),
        "--batch-size",
        str(batch_size),
        "--timeout",
        str(schwab_timeout),
        "--output-dir",
        str(paths.market_snapshot),
    ]
    if ecfg:
        snapshot_values.extend(["--ecfg", ecfg])

    build_command = (
        python_executable,
        str(probes / "build_daily_universe.py"),
        "--symbol-directory",
        str(paths.symbol_directory / "normalized_all.csv"),
        "--market-data",
        str(paths.market_snapshot / "market_data_snapshot.csv"),
        "--session-date",
        session_date.isoformat(),
        "--target-date",
        target_date.isoformat(),
        "--minimum-volume",
        minimum_volume,
        "--minimum-close",
        minimum_close,
        "--minimum-market-cap",
        minimum_market_cap,
        "--maximum-market-cap",
        maximum_market_cap,
        "--output-dir",
        str(paths.universe),
    )
    opening_command = (
        python_executable,
        str(probes / "build_opening_sampling_hierarchy.py"),
        str(paths.universe),
        "--output",
        str(paths.opening_hierarchy),
    )
    stages = [
        WorkflowStage("Nasdaq symbol directory", directory_command),
        WorkflowStage("Schwab post-close snapshot", tuple(snapshot_values)),
        WorkflowStage("Deterministic universe build", build_command),
        WorkflowStage("Opening schema-v2 r0 proposal", opening_command),
    ]
    if publish_database is not None:
        stages.append(
            WorkflowStage(
                "Opening schema-v2 r0 publication",
                (
                    python_executable,
                    str(probes / "publish_sampling_hierarchy.py"),
                    str(publish_database),
                    str(paths.opening_hierarchy),
                ),
            )
        )
    return tuple(stages)


Runner = Callable[..., subprocess.CompletedProcess[object]]


def run_stages(
    stages: Sequence[WorkflowStage],
    *,
    repository_root: str | Path,
    runner: Runner = subprocess.run,
) -> None:
    for index, stage in enumerate(stages, start=1):
        print(f"\n[{index}/{len(stages)}] {stage.name}", flush=True)
        result = runner(
            list(stage.command),
            cwd=Path(repository_root),
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"stage {index} ({stage.name}) failed with "
                f"exit code {result.returncode}"
            )


def write_workflow_manifest(
    *,
    paths: WorkflowPaths,
    stages: Sequence[WorkflowStage],
    session_date: date,
    target_date: date,
    created_at_utc: datetime | None = None,
) -> Path:
    expected = {
        "symbol_directory_manifest": (
            paths.symbol_directory / "manifest.json"
        ),
        "market_snapshot_manifest": paths.market_snapshot / "manifest.json",
        "universe_manifest": paths.universe / "manifest.json",
        "decision_ledger": paths.universe / "decision_ledger.csv",
        "uni_watchlist": paths.universe / "uni_watchlist.csv",
        "uni_symbols": paths.universe / "uni_symbols.csv",
        "opening_hierarchy_r0": paths.opening_hierarchy,
    }
    missing = [str(path) for path in expected.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "workflow completed without expected artifacts: "
            + ", ".join(missing)
        )

    with expected["universe_manifest"].open(encoding="utf-8") as file:
        universe_manifest = json.load(file)
    created = created_at_utc or datetime.now(timezone.utc)
    if created.tzinfo is None or created.utcoffset() is None:
        raise ValueError("created_at_utc must be timezone-aware")

    manifest = {
        "workflow_version": WORKFLOW_VERSION,
        "created_at_utc": created.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "session_date": session_date.isoformat(),
        "target_date": target_date.isoformat(),
        "counts": universe_manifest.get("counts", {}),
        "stages": [
            {"name": stage.name, "command": list(stage.command)}
            for stage in stages
        ],
        "artifacts": {
            name: {
                "path": path.relative_to(paths.root).as_posix(),
                "sha256": sha256_file(path),
            }
            for name, path in expected.items()
        },
    }
    with paths.manifest.open("x", encoding="utf-8") as file:
        json.dump(manifest, file, indent=2, sort_keys=True)
        file.write("\n")
    return paths.manifest
