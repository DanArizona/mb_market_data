from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from mb_market_data.observation_overlay import ObservationOverlayProjector
from mb_market_data.overlay_halts import (
    OverlayHaltInterval,
    OverlayHaltResumption,
    load_daily_nasdaq_halt_events,
    load_overlay_halt_intervals,
    merge_live_halt_intervals,
)
from test_observation_overlay import cache


class TestOverlayHalts(unittest.TestCase):
    def test_daily_nasdaq_episodes_use_first_observation_not_release(self) -> None:
        session = date(2026, 11, 13)
        records = {
            "ONFO@2026-11-13T14:31:09.153000+00:00": {
                "first_observed_at": "2026-11-13T14:32:03.368991+00:00",
                "last_observed_at": "2026-11-13T20:59:03+00:00",
                "record": {
                    "symbol": "ONFO", "halt_date": "11/13/2026",
                    "halt_time": "09:31:09.153", "reason_code": "LUDP",
                    "retrieval_mode": "CURRENT",
                    "resumption_date": "11/13/2026",
                    "resumption_quote_time": "09:31:09",
                    "resumption_trade_time": "09:41:09",
                },
            },
            "OTHER@2026-11-13T14:31:09+00:00": {"record": {"symbol": "OTHER"}},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.json"
            path.write_text(json.dumps(records), encoding="utf-8")
            intervals = load_daily_nasdaq_halt_events(
                path, symbol="ONFO", session_date=session
            )
        self.assertEqual(len(intervals), 1)
        event = intervals[0]
        self.assertEqual(event.start_et.strftime("%H:%M:%S.%f"), "09:31:09.153000")
        self.assertEqual(event.end_et.strftime("%H:%M:%S"), "09:41:09")
        # With no retained captures, the final receipt is a conservative bound;
        # the later reported end is still hidden at first observation.
        self.assertEqual(event.end_observed_at_utc.isoformat(), "2026-11-13T20:59:03+00:00")
        early = event.visible_at(event.first_observed_at_utc)
        self.assertIsNone(early.end_et)
        self.assertEqual(early.resumption_updates, ())
        self.assertEqual(event.reason_code, "LUDP")
        self.assertEqual(event.first_observed_at_utc.isoformat(),
                         "2026-11-13T14:32:03.368991+00:00")

    def test_daily_legacy_captures_recover_causal_resumption_revisions(self) -> None:
        from unittest.mock import patch
        from mb_market_data.overlay_halts import ET
        session = date(2026, 11, 13)
        record = {
            "symbol": "ZYN", "halt_date": "11/13/2026",
            "halt_time": "09:31:09", "reason_code": "LUDP",
            "retrieval_mode": "HISTORICAL", "resumption_date": "11/13/2026",
            "resumption_trade_time": "09:43:09",
        }
        episode = {
            "first_observed_at": "2026-11-13T14:32:03+00:00",
            "last_observed_at": "2026-11-13T15:30:00+00:00",
            "record": record,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.json"
            path.write_text(json.dumps({"ZYN@2026-11-13T14:31:09+00:00": episode}))
            captures = path.parent / "captures"
            captures.mkdir()
            for index, (observed, end) in enumerate((
                ("14:35:00", "09:41:09"),
                ("14:39:00", "09:46:09"),
                ("14:42:00", "09:43:09"),
            )):
                (captures / f"capture-{index}.json").write_text(json.dumps({
                    "mode": "HISTORICAL", "observed_at": f"2026-11-13T{observed}+00:00",
                    "records": [{**record, "resumption_trade_time": end}],
                }))
            original = Path.read_text
            def temporarily_locked(candidate, *args, **kwargs):
                if candidate == captures / "capture-0.json":
                    raise PermissionError("synthetic Windows publication lock")
                return original(candidate, *args, **kwargs)
            with patch.object(Path, "read_text", temporarily_locked):
                partial, = load_daily_nasdaq_halt_events(path, symbol="ZYN", session_date=session)
                self.assertEqual(len(partial.resumption_updates), 2)
            event, = load_daily_nasdaq_halt_events(path, symbol="ZYN", session_date=session)
            self.assertEqual(len(event.resumption_updates), 3)
            self.assertEqual(event.end_observed_at_utc.hour, 14)
            self.assertEqual(event.end_observed_at_utc.minute, 42)
            for minute, end, update_count in ((34, None, 0), (36, "09:41:09", 1), (40, "09:46:09", 2), (43, "09:43:09", 3)):
                visible = event.visible_at(datetime(2026, 11, 13, 14, minute, tzinfo=timezone.utc))
                self.assertEqual(visible.end_et.strftime("%H:%M:%S") if visible.end_et else None, end)
                self.assertEqual(len(visible.resumption_updates), update_count)
            # Repeated symbol/refresh reads do not reopen the capture archive.
            def checked_read(candidate, *args, **kwargs):
                self.assertNotEqual(candidate.parent, captures)
                return original(candidate, *args, **kwargs)
            with patch.object(Path, "read_text", checked_read):
                self.assertEqual(load_daily_nasdaq_halt_events(path, symbol="ZYN", session_date=session), (event,))
            self.assertEqual(event.latest_observed_at_utc, datetime(2026, 11, 13, 14, 42, tzinfo=timezone.utc))
            self.assertEqual(event.start_et, datetime(2026, 11, 13, 9, 31, 9, tzinfo=ET))

    def test_daily_persisted_resumption_history_accepts_scheduled_end_and_rejects_bad_receipts(self) -> None:
        from copy import deepcopy
        session = date(2026, 11, 13)
        episode = {
            "first_observed_at": "2026-11-13T14:32:03+00:00",
            "last_observed_at": "2026-11-13T14:35:00+00:00",
            "record": {
                "symbol": "ZYN", "halt_date": "11/13/2026", "halt_time": "09:31:09",
                "reason_code": "LUDP", "retrieval_mode": "HISTORICAL",
                "resumption_date": "11/13/2026", "resumption_trade_time": "09:41:09",
            },
            "resumption_history_complete": True,
            "resumption_updates": [{
                "resumption_date": "11/13/2026", "resumption_trade_time": "09:41:09",
                "observed_at": "2026-11-13T14:34:00+00:00",
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.json"
            def load(value):
                path.write_text(json.dumps({"ZYN@2026-11-13T14:31:09+00:00": value}))
                return load_daily_nasdaq_halt_events(path, symbol="ZYN", session_date=session)
            event, = load(episode)
            self.assertLess(event.end_observed_at_utc, event.end_et)
            self.assertIsNone(event.visible_at(datetime(2026, 11, 13, 14, 32, tzinfo=timezone.utc)))
            for bad_time in ("2026-11-13T14:31:00+00:00", "2026-11-13T14:36:00+00:00", "2026-11-13T14:34:00"):
                bad = deepcopy(episode)
                bad["resumption_updates"][0]["observed_at"] = bad_time
                with self.subTest(bad_time=bad_time), self.assertRaises(ValueError):
                    load(bad)
            bad = deepcopy(episode)
            bad["record"]["retrieval_mode"] = "UNKNOWN"
            with self.assertRaisesRegex(ValueError, "retrieval mode"):
                load(bad)

    def test_quote_resumption_alone_does_not_end_trade_halt(self) -> None:
        episode = {
            "first_observed_at": "2026-11-13T14:32:03+00:00",
            "last_observed_at": "2026-11-13T14:35:00+00:00",
            "record": {
                "symbol": "ZYN", "halt_date": "11/13/2026", "halt_time": "09:31:09",
                "reason_code": "LUDP", "retrieval_mode": "CURRENT",
                "resumption_date": "11/13/2026", "resumption_quote_time": "09:34:00",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.json"
            path.write_text(json.dumps({"ZYN@2026-11-13T14:31:09+00:00": episode}))
            event, = load_daily_nasdaq_halt_events(path, symbol="ZYN", session_date=date(2026, 11, 13))
        self.assertIsNone(event.end_et)
        self.assertEqual(event.resumption_updates, ())

    def test_live_merge_retains_earlier_fallback_receipt_and_real_revisions(self) -> None:
        from dataclasses import replace
        from mb_market_data.overlay_halts import ET
        start = datetime(2026, 11, 13, 9, 31, 9, tzinfo=ET)
        first = datetime(2026, 11, 13, 14, 32, tzinfo=timezone.utc)
        end = datetime(2026, 11, 13, 9, 41, 9, tzinfo=ET)
        early = datetime(2026, 11, 13, 14, 40, tzinfo=timezone.utc)
        later = datetime(2026, 11, 13, 14, 45, tzinfo=timezone.utc)
        original = OverlayHaltInterval(start, end, "LUDP", first, early,
                                       (OverlayHaltResumption(end, early),))
        repeated = replace(original, end_observed_at_utc=later,
                           resumption_updates=(OverlayHaltResumption(end, later),))
        self.assertEqual(merge_live_halt_intervals((original,), (repeated,)), (original,))
        revised_end = end.replace(minute=46)
        revised = replace(repeated, end_et=revised_end,
                          resumption_updates=(OverlayHaltResumption(revised_end, later),))
        merged, = merge_live_halt_intervals((original,), (revised,))
        self.assertEqual([u.end_et for u in merged.resumption_updates], [end, revised_end])
        self.assertEqual(merged.visible_at(early).end_et, end)
        self.assertEqual(merged.visible_at(later).end_et, revised_end)
        for incoming in ((), (replace(repeated, first_observed_at_utc=later),),
                         (replace(repeated, end_et=None, end_observed_at_utc=None, resumption_updates=()),)):
            with self.subTest(incoming=incoming), self.assertRaises(ValueError):
                merge_live_halt_intervals((original,), incoming)
        conflicting = replace(original, end_et=revised_end,
                              resumption_updates=(OverlayHaltResumption(revised_end, early),))
        with self.assertRaisesRegex(ValueError, "conflicts"):
            merge_live_halt_intervals((original,), (conflicting,))

    def test_daily_halt_skips_prior_day_episode_but_keeps_current_episode(self) -> None:
        """Nasdaq's CURRENT snapshot may carry an older JAGX halt episode."""
        session = date(2026, 11, 24)
        previous = {
            "first_observed_at": "2026-11-24T14:30:00+00:00",
            "last_observed_at": "2026-11-24T14:32:00+00:00",
            "record": {
                "symbol": "JAGX", "halt_date": "11/23/2026",
                "halt_time": "19:50:09", "reason_code": "LUDP",
                "retrieval_mode": "CURRENT",
            },
        }
        current = {
            "first_observed_at": "2026-11-24T14:32:03+00:00",
            "last_observed_at": "2026-11-24T14:33:03+00:00",
            "record": {
                "symbol": "JAGX", "halt_date": "11/24/2026",
                "halt_time": "09:31:09.153", "reason_code": "M",
                "retrieval_mode": "CURRENT",
            },
        }
        episodes = {
            # 19:50 ET on Nov 23 is 00:50 UTC on Nov 24: same UTC day,
            # different Eastern session. This matches the daily source date gate.
            "JAGX@2026-11-24T00:50:09+00:00": previous,
            "JAGX@2026-11-24T14:31:09.153+00:00": current,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.json"
            path.write_text(json.dumps(episodes), encoding="utf-8")
            intervals = load_daily_nasdaq_halt_events(
                path, symbol="JAGX", session_date=session
            )
        self.assertEqual(len(intervals), 1)
        self.assertEqual(intervals[0].reason_code, "M")
        self.assertEqual(intervals[0].start_et.date(), session)
        self.assertEqual(
            intervals[0].first_observed_at_utc,
            datetime(2026, 11, 24, 14, 32, 3, tzinfo=timezone.utc),
        )

    def test_prior_day_only_does_not_invent_a_current_day_halt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.json"
            path.write_text(json.dumps({
                "JAGX@2026-11-24T00:50:09+00:00": {
                    "record": {
                        "symbol": "JAGX", "halt_date": "11/23/2026",
                        "halt_time": "19:50:09", "reason_code": "LUDP",
                        "retrieval_mode": "CURRENT",
                    }
                }
            }), encoding="utf-8")
            result = load_daily_nasdaq_halt_events(
                path, symbol="JAGX", session_date=date(2026, 11, 24)
            )
        self.assertEqual(result, ())

    def test_daily_halt_still_rejects_inconsistent_key_record_date(self) -> None:
        session = date(2026, 11, 24)
        for key, record_day, expected in (
            ("JAGX@2026-11-24T14:31:09+00:00", "11/23/2026",
             "belongs to a different session"),
            ("JAGX@2026-11-24T00:50:09+00:00", "11/24/2026",
             "key differs from halt date"),
            ("JAGX@2026-11-25T14:31:09+00:00", "11/25/2026",
             "future session"),
        ):
            with self.subTest(key=key):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "events.json"
                    path.write_text(json.dumps({
                        key: {
                            "record": {
                                "symbol": "JAGX", "halt_date": record_day,
                                "halt_time": "09:31:09", "reason_code": "LUDP",
                                "retrieval_mode": "CURRENT",
                            }
                        },
                    }), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, expected):
                        load_daily_nasdaq_halt_events(
                            path, symbol="JAGX", session_date=session
                        )

    def test_daily_halt_rejects_mismatched_key_and_event_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.json"
            path.write_text(json.dumps({
                "ONFO@2026-11-13T14:30:00+00:00": {
                    "first_observed_at": "2026-11-13T14:32:03+00:00",
                    "last_observed_at": "2026-11-13T14:32:03+00:00",
                    "record": {
                        "symbol": "ONFO", "halt_date": "11/13/2026",
                        "halt_time": "09:31:09.153", "reason_code": "LUDP",
                        "retrieval_mode": "CURRENT",
                    },
                },
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "key differs"):
                load_daily_nasdaq_halt_events(
                    path, symbol="ONFO", session_date=date(2026, 11, 13)
                )

    def test_daily_halt_not_revealed_before_first_observation(self) -> None:
        from mb_market_data.observation_overlay import ET
        session = date(2026, 9, 24)
        event = OverlayHaltInterval(
            datetime(2026, 9, 24, 9, 31, 9, tzinfo=ET),
            None,
            "LUDP",
            datetime(2026, 9, 24, 13, 32, 3, tzinfo=timezone.utc),
        )
        projector = ObservationOverlayProjector(
            cache=cache(), symbol="TEST", session_date=session,
            halt_intervals=(event,),
        )
        before = datetime(2026, 9, 24, 13, 32, 2, tzinfo=timezone.utc)
        observed = datetime(2026, 9, 24, 13, 32, 3, tzinfo=timezone.utc)
        self.assertEqual(projector.snapshot(before).halt_intervals, ())
        self.assertEqual(projector.snapshot(observed).halt_intervals, (event,))

    def test_quote_only_projector_has_no_fabricated_candles(self) -> None:
        session = date(2026, 9, 24)
        projector = ObservationOverlayProjector(
            cache=None, symbol="TEST", session_date=session,
        )
        state = projector.snapshot(
            datetime(2026, 9, 24, 13, 35, tzinfo=timezone.utc)
        )
        self.assertIsNone(state.cache)
        self.assertEqual(state.candles, ())

    def test_confirmed_knrx_halt_does_not_mark_prior_gap(self) -> None:
        session = date(2026, 9, 28)
        record = {
            "symbol": "KNRX", "halt_date": "09/28/2026",
            "halt_time": "09:36:48", "reason_code": "M",
            "resumption_date": "09/28/2026",
            "resumption_trade_time": "09:41:48",
            "retrieval_mode": "HISTORICAL",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "normalized.json"
            path.write_text(json.dumps([record, {**record, "symbol": "OTHER"}]))
            intervals = load_overlay_halt_intervals(
                path, symbol="KNRX", session_date=session
            )
        self.assertEqual(
            [
                (
                    interval.start_et.strftime("%H:%M:%S"),
                    interval.end_et.strftime("%H:%M:%S"),
                )
                for interval in intervals
            ],
            [("09:36:48", "09:41:48")],
        )
        self.assertFalse(
            any(i.start_et.hour == 9 and i.start_et.minute < 36 for i in intervals)
        )

    def test_projector_exposes_markers_only_after_event_time(self) -> None:
        # Reuse the cache fixture's session and symbol.
        from mb_market_data.observation_overlay import ET
        session = date(2026, 9, 24)
        interval = OverlayHaltInterval(
            datetime(2026, 9, 24, 9, 36, 48, tzinfo=ET),
            datetime(2026, 9, 24, 9, 41, 48, tzinfo=ET),
            "M",
        )
        projector = ObservationOverlayProjector(
            cache=cache(),
            symbol="TEST",
            session_date=session,
            halt_intervals=(interval,),
        )
        before = datetime(2026, 9, 24, 13, 36, 47, tzinfo=timezone.utc)
        at = datetime(2026, 9, 24, 13, 36, 48, tzinfo=timezone.utc)
        self.assertEqual(projector.snapshot(before).halt_intervals, ())
        self.assertEqual(projector.snapshot(at).halt_intervals, (interval,))


if __name__ == "__main__":
    unittest.main()
