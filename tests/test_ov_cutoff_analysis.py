from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from mb_market_data.api_overnight_volume import (
    ET,
    APIOvernightVolumeBatch,
    APIOvernightVolumeCandle,
    APIOvernightVolumeObservation,
    APIOvernightVolumeStatus,
    sha256_file,
)
from mb_market_data.ov_cutoff_analysis import (
    ProductionOVBaseline,
    analyze_cutoff_batch,
    calculate_cutoff_volumes,
    load_production_ov_baseline,
    write_cutoff_analysis_artifacts,
)
from mb_market_data.sampling_membership import SamplingHierarchyRevision


UTC = timezone.utc
TRADE_DATE = date(2026, 9, 24)


def candle(hour: int, minute: int, volume: int) -> APIOvernightVolumeCandle:
    return APIOvernightVolumeCandle(
        start_et=datetime(2026, 9, 24, hour, minute, tzinfo=ET),
        open=1.0,
        high=1.1,
        low=0.9,
        close=1.0,
        volume=volume,
    )


def observation(
    symbol: str,
    candles: tuple[APIOvernightVolumeCandle, ...],
) -> APIOvernightVolumeObservation:
    started = datetime(2026, 9, 24, 20, 0, tzinfo=UTC)
    return APIOvernightVolumeObservation(
        symbol=symbol,
        trade_date=TRADE_DATE,
        window_start_et=datetime(2026, 9, 24, 0, 0, tzinfo=ET),
        window_end_et=datetime(2026, 9, 24, 9, 30, tzinfo=ET),
        status=APIOvernightVolumeStatus.OK,
        ov_decision=sum(item.volume for item in candles),
        candle_count=len(candles),
        attempts=1,
        request_started_at_utc=started,
        response_received_at_utc=started + timedelta(seconds=1),
        http_status=200,
        detail=None,
        candles=candles,
    )


def batch(
    observations: tuple[APIOvernightVolumeObservation, ...],
) -> APIOvernightVolumeBatch:
    started = datetime(2026, 9, 24, 20, 0, tzinfo=UTC)
    return APIOvernightVolumeBatch(
        trade_date=TRADE_DATE,
        window_start_et=datetime(2026, 9, 24, 0, 0, tzinfo=ET),
        window_end_et=datetime(2026, 9, 24, 9, 30, tzinfo=ET),
        started_at_utc=started,
        completed_at_utc=started + timedelta(minutes=1),
        request_interval_seconds=0.5,
        max_attempts=3,
        observations=observations,
    )


def opening(symbols: tuple[str, ...]) -> SamplingHierarchyRevision:
    return SamplingHierarchyRevision(
        session_date=TRADE_DATE,
        revision=0,
        effective_at=datetime(2026, 9, 24, 9, 30, tzinfo=ET),
        uni_symbols=symbols,
        focus_symbols=(),
        hot_symbols=(),
        source="unit-test",
    )


class TestCutoffCalculation(unittest.TestCase):
    def test_uses_half_open_five_minute_landmarks(self) -> None:
        candles = tuple(
            candle(hour, minute, volume)
            for hour, minute, volume in (
                (0, 0, 1),
                (8, 20, 2),
                (8, 25, 3),
                (8, 55, 4),
                (9, 0, 5),
                (9, 10, 6),
                (9, 15, 7),
                (9, 20, 8),
                (9, 25, 9),
            )
        )

        values = calculate_cutoff_volumes(candles, trade_date=TRADE_DATE)

        self.assertEqual(
            values,
            {
                "ov_0825": 3,
                "ov_0900": 10,
                "ov_0915": 21,
                "ov_0925": 36,
                "ov_final": 45,
            },
        )


class TestCutoffAnalysis(unittest.TestCase):
    def test_ranks_deterministically_and_reports_transitions(self) -> None:
        observations = (
            observation("AAA", (candle(8, 20, 100),)),
            observation(
                "BBB", (candle(8, 20, 90), candle(8, 25, 20))
            ),
            observation(
                "CCC", (candle(8, 20, 80), candle(9, 0, 50))
            ),
        )
        baseline = ProductionOVBaseline(
            manifest_path=Path("baseline.json"),
            manifest_sha256="a" * 64,
            observations_path=Path("baseline.csv"),
            observations_sha256="b" * 64,
            cutoff_name="ov_0825",
            cutoff_et=time(8, 25),
            values={"AAA": 100, "BBB": 90, "CCC": 80},
        )

        result = analyze_cutoff_batch(
            batch(observations),
            opening_symbols=("AAA", "BBB", "CCC"),
            baseline=baseline,
            limit=2,
        )

        self.assertEqual(result.membership["ov_0825"], ("AAA", "BBB"))
        self.assertEqual(result.membership["ov_0900"], ("BBB", "AAA"))
        self.assertEqual(result.membership["ov_0915"], ("CCC", "BBB"))
        transition = result.transitions[1]
        self.assertEqual(transition["overlap_count"], 1)
        self.assertEqual(transition["entrants"], ["CCC"])
        self.assertEqual(transition["exits"], ["AAA"])
        self.assertTrue(result.complete)

    def test_records_baseline_mismatch(self) -> None:
        observations = (observation("AAA", (candle(8, 20, 100),)),)
        baseline = ProductionOVBaseline(
            manifest_path=Path("baseline.json"),
            manifest_sha256="a" * 64,
            observations_path=Path("baseline.csv"),
            observations_sha256="b" * 64,
            cutoff_name="ov_0825",
            cutoff_et=time(8, 25),
            values={"AAA": 99},
        )

        result = analyze_cutoff_batch(
            batch(observations),
            opening_symbols=("AAA",),
            baseline=baseline,
            limit=1,
        )

        self.assertEqual(result.baseline_mismatch_symbols, ("AAA",))
        self.assertFalse(result.complete)


class TestCutoffEvidence(unittest.TestCase):
    def test_loads_verified_baseline_and_writes_analysis_bundle(self) -> None:
        revision = opening(("AAA",))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            opening_path = root / "opening.json"
            opening_path.write_text("{}\n", encoding="utf-8")
            observations_path = root / "api_ov_observations.csv"
            with observations_path.open(
                "w", encoding="utf-8", newline=""
            ) as output:
                writer = csv.DictWriter(
                    output,
                    fieldnames=("symbol", "status", "ov_decision"),
                )
                writer.writeheader()
                writer.writerow(
                    {"symbol": "AAA", "status": "ok", "ov_decision": 100}
                )
            manifest_path = root / "production_manifest.json"
            manifest = {
                "session_date": "2026-09-24",
                "window_end_et": "2026-09-24T09:00:00-04:00",
                "opening_uni_count": 1,
                "complete_opening_uni": True,
                "production_eligible": True,
                "opening": {"content_sha256": revision.content_sha256},
                "artifacts": {
                    "api_ov_observations": {
                        "path": observations_path.name,
                        "sha256": sha256_file(observations_path),
                    }
                },
            }
            manifest_path.write_text(
                json.dumps(manifest) + "\n", encoding="utf-8"
            )

            baseline = load_production_ov_baseline(manifest_path, revision)
            self.assertEqual(baseline.cutoff_name, "ov_0900")
            self.assertEqual(baseline.cutoff_et, time(9, 0))
            acquired = batch(
                (observation("AAA", (candle(8, 20, 100), candle(9, 25, 5))),)
            )
            analysis = analyze_cutoff_batch(
                acquired,
                opening_symbols=("AAA",),
                baseline=baseline,
                limit=1,
            )
            artifacts = write_cutoff_analysis_artifacts(
                root / "analysis",
                opening_proposal_path=opening_path,
                opening=revision,
                baseline=baseline,
                batch=acquired,
                analysis=analysis,
            )
            result_manifest = json.loads(
                artifacts.manifest.read_text(encoding="utf-8")
            )

            self.assertTrue(result_manifest["analysis_complete"])
            self.assertEqual(
                result_manifest["production_baseline_cutoff"], "09:00"
            )
            self.assertEqual(result_manifest["baseline_match_count"], 1)
            self.assertEqual(result_manifest["baseline_mismatch_count"], 0)
            self.assertTrue(artifacts.metrics.is_file())
            self.assertTrue(artifacts.membership.is_file())
            self.assertTrue(artifacts.candles.is_file())


if __name__ == "__main__":
    unittest.main()
