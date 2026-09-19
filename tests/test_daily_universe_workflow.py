from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from mb_market_data.daily_universe_workflow import (
    WorkflowStage,
    build_stage_commands,
    run_stages,
    workflow_paths,
    write_workflow_manifest,
)


class TestDailyUniverseWorkflow(unittest.TestCase):
    def test_builds_three_explicit_stage_commands(self) -> None:
        paths = workflow_paths("output/workflow")
        stages = build_stage_commands(
            repository_root="C:/repo",
            python_executable="python",
            paths=paths,
            session_date=date(2026, 9, 18),
            target_date=date(2026, 9, 21),
            ecfg="C:/vault/secure.ecfg",
        )

        self.assertEqual(
            [stage.name for stage in stages],
            [
                "Nasdaq symbol directory",
                "Schwab post-close snapshot",
                "Deterministic universe build",
            ],
        )
        self.assertIn("--output-dir", stages[0].command)
        self.assertIn("candidate_non_etf_non_test.csv", " ".join(stages[1].command))
        self.assertIn("--ecfg", stages[1].command)
        self.assertIn("normalized_all.csv", " ".join(stages[2].command))
        self.assertIn("2026-09-21", stages[2].command)

    def test_stops_after_a_failed_stage(self) -> None:
        stages = (
            WorkflowStage("one", ("python", "one.py")),
            WorkflowStage("two", ("python", "two.py")),
        )
        commands: list[list[str]] = []

        def runner(command: list[str], **_: object) -> SimpleNamespace:
            commands.append(command)
            return SimpleNamespace(returncode=7)

        with self.assertRaisesRegex(RuntimeError, "stage 1.*exit code 7"):
            run_stages(stages, repository_root=".", runner=runner)
        self.assertEqual(commands, [["python", "one.py"]])

    def test_rejects_bad_filters_before_building_commands(self) -> None:
        with self.assertRaisesRegex(ValueError, "decimal numbers"):
            build_stage_commands(
                repository_root="C:/repo",
                python_executable="python",
                paths=workflow_paths("output/workflow"),
                session_date=date(2026, 9, 18),
                target_date=date(2026, 9, 21),
                minimum_volume="not-a-number",
            )

    def test_writes_top_level_manifest_from_stage_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = workflow_paths(Path(temporary) / "workflow")
            paths.symbol_directory.mkdir(parents=True)
            paths.market_snapshot.mkdir()
            paths.universe.mkdir()
            (paths.symbol_directory / "manifest.json").write_text(
                "{}\n", encoding="utf-8"
            )
            (paths.market_snapshot / "manifest.json").write_text(
                "{}\n", encoding="utf-8"
            )
            (paths.universe / "manifest.json").write_text(
                json.dumps({"counts": {"included": 554}}),
                encoding="utf-8",
            )
            for filename in (
                "decision_ledger.csv",
                "uni_watchlist.csv",
                "uni_symbols.csv",
            ):
                (paths.universe / filename).write_text(
                    "symbol\nDAIC\n", encoding="utf-8"
                )
            stages = (WorkflowStage("example", ("python", "example.py")),)

            manifest_path = write_workflow_manifest(
                paths=paths,
                stages=stages,
                session_date=date(2026, 9, 18),
                target_date=date(2026, 9, 21),
                created_at_utc=datetime(
                    2026, 9, 19, 1, 0, tzinfo=timezone.utc
                ),
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["counts"]["included"], 554)
        self.assertEqual(manifest["target_date"], "2026-09-21")
        self.assertEqual(
            manifest["artifacts"]["uni_watchlist"]["path"],
            "universe/uni_watchlist.csv",
        )

    def test_plan_command_requires_no_network_or_credentials(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(
                None,
                [
                    str(repository_root / "src"),
                    environment.get("PYTHONPATH", ""),
                ],
            )
        )
        result = subprocess.run(
            [
                sys.executable,
                str(
                    repository_root
                    / "probes"
                    / "run_daily_universe_production.py"
                ),
                "--session-date",
                "2026-09-18",
                "--target-date",
                "2026-09-21",
                "--plan",
            ],
            cwd=repository_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Daily universe production plan", result.stdout)
        self.assertIn("[1/3] Nasdaq symbol directory", result.stdout)
        self.assertIn("[3/3] Deterministic universe build", result.stdout)


if __name__ == "__main__":
    unittest.main()
