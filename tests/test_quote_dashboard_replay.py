from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from mb_market_data.observation_overlay import (
    ET as OVERLAY_ET,
    MembershipBand,
    ObservationOverlayOHLCVCache,
    ObservationOverlayProjector,
    OverlayCandle,
)
from mb_market_data.quote_dashboard_replay import (
    DashboardReplayStatus,
    QuoteDashboardReplayController,
)
from mb_market_data.overlay_halts import OverlayHaltInterval, OverlayHaltResumption
from mb_market_data.quote_journal_replay import (
    ChannelRevisionEvent,
    MembershipRevisionEvent,
    ReplayTimeline,
)
from mb_market_data.quote_observation_store import SamplingChannelRevision
from mb_market_data.sampling_membership import SamplingHierarchyRevision

UTC = timezone.utc
SESSION_DATE = date(2026, 9, 11)
START = datetime(2026, 9, 11, 13, 30, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def revision_event(channel: str, seconds: int) -> ChannelRevisionEvent:
    return ChannelRevisionEvent(
        SamplingChannelRevision(
            channel=channel,
            session_date=SESSION_DATE,
            revision=0,
            effective_at=START + timedelta(seconds=seconds),
            symbols=(channel.upper(),),
            source="unit-test",
        )
    )


def membership_event(seconds: int) -> MembershipRevisionEvent:
    return MembershipRevisionEvent(
        SamplingHierarchyRevision(
            session_date=SESSION_DATE,
            revision=0,
            effective_at=START + timedelta(seconds=seconds),
            uni_symbols=("FOCUS", "HOT", "UNI"),
            focus_symbols=("FOCUS", "HOT"),
            hot_symbols=("HOT",),
            source="unit-test",
        )
    )


def overlay_projector() -> ObservationOverlayProjector:
    return ObservationOverlayProjector(
        cache=ObservationOverlayOHLCVCache(
            symbol="FOCUS",
            session_date=SESSION_DATE,
            provider="Schwab",
            source="unit-test",
            acquired_at_utc=START + timedelta(hours=8),
            request_start_et=datetime(2026, 9, 11, 0, 0, tzinfo=OVERLAY_ET),
            request_end_et=datetime(2026, 9, 11, 16, 5, tzinfo=OVERLAY_ET),
            source_payload_sha256="a" * 64,
            candles=(),
        ),
        symbol="FOCUS",
        session_date=SESSION_DATE,
    )


class TestQuoteDashboardReplayController(unittest.TestCase):
    def test_historical_seek_reveals_resumption_updates_only_at_their_receipts(self) -> None:
        first = START + timedelta(minutes=2)
        first_resume = OverlayHaltResumption(START + timedelta(minutes=6), first + timedelta(seconds=10))
        revised_resume = OverlayHaltResumption(START + timedelta(minutes=8), first + timedelta(minutes=1))
        halt = OverlayHaltInterval(
            START.astimezone(OVERLAY_ET), revised_resume.end_et, "LUDP", first,
            revised_resume.observed_at_utc, (first_resume, revised_resume),
        )
        event = membership_event(0)
        controller = QuoteDashboardReplayController(
            timeline=ReplayTimeline(SESSION_DATE, 1, START, START),
            event_factory=lambda: iter((event,)),
            overlay_projector_factory=lambda: ObservationOverlayProjector(
                cache=None, symbol="FOCUS", session_date=SESSION_DATE, halt_intervals=(halt,),
            ),
            overlay_end_at_utc=START + timedelta(minutes=10),
        )
        self.assertEqual(controller.seek(first - timedelta(seconds=1)).observation_overlay.halt_intervals, ())
        self.assertIsNone(controller.seek(first).observation_overlay.halt_intervals[0].end_et)
        known = controller.seek(first_resume.observed_at_utc).observation_overlay.halt_intervals[0]
        self.assertEqual(known.end_et, first_resume.end_et)
        self.assertEqual(known.resumption_updates, (first_resume,))
        self.assertEqual(controller.seek(revised_resume.observed_at_utc).observation_overlay.halt_intervals[0].end_et,
                         revised_resume.end_et)
        self.assertIsNone(controller.seek(first).observation_overlay.halt_intervals[0].end_et)
        with self.assertRaisesRegex(RuntimeError, "Real Time"):
            controller.install_live_halt_intervals((halt,), symbol="FOCUS", session_date=SESSION_DATE)
        for invalid in (
            replace(halt, resumption_updates=(revised_resume, first_resume)),
            replace(halt, end_et=first_resume.end_et),
            replace(halt, resumption_updates=(), end_observed_at_utc=None),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ObservationOverlayProjector(cache=None, symbol="FOCUS", session_date=SESSION_DATE,
                                            halt_intervals=(invalid,))

    def test_switch_to_another_symbol_does_not_reuse_wrong_ohlcv_cache(self) -> None:
        event = membership_event(0)
        matching_cache = overlay_projector().cache
        self.assertIsNotNone(matching_cache)

        def projector(symbol: str) -> ObservationOverlayProjector:
            return ObservationOverlayProjector(
                cache=matching_cache if symbol == "FOCUS" else None,
                symbol=symbol,
                session_date=SESSION_DATE,
            )

        controller = QuoteDashboardReplayController(
            timeline=ReplayTimeline(SESSION_DATE, 1, START, START),
            event_factory=lambda: iter((event,)),
            overlay_projector_factory=lambda: projector("FOCUS"),
            symbol_projector_factory=projector,
        )
        controller.finish()
        self.assertIsNotNone(controller.snapshot().observation_overlay.cache)
        other = controller.select_overlay_symbol("HOT")
        self.assertEqual(other.observation_overlay.symbol, "HOT")
        self.assertIsNone(other.observation_overlay.cache)
        back = controller.select_overlay_symbol("FOCUS")
        self.assertEqual(back.observation_overlay.cache.symbol, "FOCUS")
        self.assertEqual(back.observation_overlay.cache.session_date, SESSION_DATE)

    def test_switch_overlay_symbol_keeps_replay_clock_and_persists_through_seek(self) -> None:
        event = membership_event(0)
        def make_projector(symbol: str) -> ObservationOverlayProjector:
            return ObservationOverlayProjector(cache=None, symbol=symbol, session_date=SESSION_DATE)
        controller = QuoteDashboardReplayController(
            timeline=ReplayTimeline(SESSION_DATE, 1, START, START),
            event_factory=lambda: iter((event,)),
            overlay_projector_factory=lambda: make_projector("FOCUS"),
            symbol_projector_factory=make_projector,
        )
        before = controller.finish()
        switched = controller.select_overlay_symbol(" hot ")
        self.assertEqual(switched.observation_overlay.symbol, "HOT")
        self.assertEqual(switched.observation_overlay.current_band, MembershipBand.HOT)
        self.assertEqual(switched.replay_time_utc, before.replay_time_utc)
        self.assertEqual(switched.applied_event_count, before.applied_event_count)
        self.assertEqual(switched.status, before.status)
        self.assertIsNone(switched.observation_overlay.cache)
        self.assertEqual(controller.restart().observation_overlay.symbol, "HOT")
        self.assertEqual(controller.finish().observation_overlay.current_band, MembershipBand.HOT)
        self.assertEqual(controller.seek(START).observation_overlay.symbol, "HOT")

    def test_rejected_symbol_switch_preserves_original_overlay(self) -> None:
        event = membership_event(0)
        controller = QuoteDashboardReplayController(
            timeline=ReplayTimeline(SESSION_DATE, 1, START, START),
            event_factory=lambda: iter((event,)),
            overlay_projector_factory=lambda: ObservationOverlayProjector(
                cache=None, symbol="FOCUS", session_date=SESSION_DATE,
            ),
            symbol_projector_factory=lambda symbol: ObservationOverlayProjector(
                cache=None, symbol="OTHER", session_date=SESSION_DATE,
            ),
        )
        controller.finish()
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            controller.select_overlay_symbol("HOT")
        self.assertEqual(controller.snapshot().observation_overlay.symbol, "FOCUS")
        with self.assertRaisesRegex(ValueError, "nonblank"):
            controller.select_overlay_symbol(" ")
        self.assertEqual(controller.snapshot().observation_overlay.symbol, "FOCUS")

    def test_quote_only_overlay_is_available_without_ohlcv_cache(self) -> None:
        event = membership_event(0)
        controller = QuoteDashboardReplayController(
            timeline=ReplayTimeline(SESSION_DATE, 1, START, START),
            event_factory=lambda: iter((event,)),
            overlay_projector_factory=lambda: ObservationOverlayProjector(
                cache=None, symbol="FOCUS", session_date=SESSION_DATE,
            ),
        )
        replay = controller.finish()
        self.assertIsNotNone(replay.observation_overlay)
        self.assertEqual(replay.observation_overlay.current_band, MembershipBand.FOCUS)
        self.assertIsNone(replay.observation_overlay.cache)
        self.assertEqual(replay.observation_overlay.candles, ())

    def test_overlay_clock_continues_after_last_journal_event(self) -> None:
        event = membership_event(0)
        clock = FakeClock()
        end = START + timedelta(seconds=20)
        controller = QuoteDashboardReplayController(
            timeline=ReplayTimeline(SESSION_DATE, 1, START, START),
            event_factory=lambda: iter((event,)),
            speed=2,
            monotonic=clock.monotonic,
            overlay_projector_factory=overlay_projector,
            overlay_end_at_utc=end,
        )

        playing = controller.play()
        self.assertEqual(playing.applied_event_count, 1)
        self.assertEqual(playing.status, DashboardReplayStatus.PLAYING)
        clock.advance(5)
        middle = controller.tick()
        self.assertEqual(middle.replay_time_utc, START + timedelta(seconds=10))
        self.assertEqual(middle.applied_event_count, 1)
        self.assertEqual(middle.status, DashboardReplayStatus.PLAYING)
        self.assertEqual(
            controller.seek(end - timedelta(seconds=1)).status,
            DashboardReplayStatus.PAUSED,
        )
        finished = controller.finish()
        self.assertEqual(finished.replay_time_utc, end)
        self.assertEqual(finished.status, DashboardReplayStatus.COMPLETE)
        self.assertEqual(finished.applied_event_count, 1)

    def test_after_hours_candle_appears_only_after_completion(self) -> None:
        event = membership_event(0)
        candle = OverlayCandle(
            symbol="FOCUS",
            start_et=datetime(2026, 9, 11, 16, 0, tzinfo=OVERLAY_ET),
            open=10.0,
            high=10.5,
            low=9.9,
            close=10.2,
            volume=100,
        )
        cache = ObservationOverlayOHLCVCache(
            symbol="FOCUS",
            session_date=SESSION_DATE,
            provider="Schwab",
            source="unit-test",
            acquired_at_utc=datetime(2026, 9, 12, 1, tzinfo=UTC),
            request_start_et=datetime(2026, 9, 11, 0, tzinfo=OVERLAY_ET),
            request_end_et=datetime(2026, 9, 11, 20, tzinfo=OVERLAY_ET),
            source_payload_sha256="a" * 64,
            candles=(candle,),
        )
        controller = QuoteDashboardReplayController(
            timeline=ReplayTimeline(SESSION_DATE, 1, START, START),
            event_factory=lambda: iter((event,)),
            overlay_projector_factory=lambda: ObservationOverlayProjector(
                cache=cache, symbol="FOCUS", session_date=SESSION_DATE
            ),
            overlay_end_at_utc=cache.request_end_et,
        )
        self.assertEqual(
            len(
                controller.seek(
                    datetime(2026, 9, 11, 16, 4, 59, tzinfo=OVERLAY_ET)
                ).observation_overlay.candles
            ),
            0,
        )
        self.assertEqual(
            len(
                controller.seek(
                    datetime(2026, 9, 11, 16, 5, tzinfo=OVERLAY_ET)
                ).observation_overlay.candles
            ),
            1,
        )
        self.assertEqual(
            controller.finish().replay_time_utc, cache.request_end_et.astimezone(UTC)
        )

    def test_optional_overlay_restarts_and_seeks_with_main_projection(self) -> None:
        event = membership_event(0)
        controller = QuoteDashboardReplayController(
            timeline=ReplayTimeline(
                session_date=SESSION_DATE,
                event_count=1,
                first_available_at_utc=event.available_at_utc,
                last_available_at_utc=event.available_at_utc,
            ),
            event_factory=lambda: iter((event,)),
            overlay_projector_factory=overlay_projector,
        )

        initial = controller.snapshot()
        self.assertIsNotNone(initial.observation_overlay)
        self.assertEqual(
            initial.observation_overlay.current_band,
            MembershipBand.OUTSIDE_UNI,
        )

        applied = controller.step()
        self.assertEqual(
            applied.observation_overlay.current_band,
            MembershipBand.FOCUS,
        )
        restarted = controller.restart()
        self.assertEqual(
            restarted.observation_overlay.current_band,
            MembershipBand.OUTSIDE_UNI,
        )
        sought = controller.seek(START)
        self.assertEqual(
            sought.observation_overlay.current_band,
            MembershipBand.FOCUS,
        )

    def test_one_step_applies_complete_hierarchy_revision(self) -> None:
        event = membership_event(0)
        controller = QuoteDashboardReplayController(
            timeline=ReplayTimeline(
                session_date=SESSION_DATE,
                event_count=1,
                first_available_at_utc=event.available_at_utc,
                last_available_at_utc=event.available_at_utc,
            ),
            event_factory=lambda: iter((event,)),
        )

        result = controller.step()

        self.assertEqual(result.applied_event_count, 1)
        self.assertEqual(result.state.channels, ("focus", "hot", "uni"))
        self.assertEqual(
            result.state.current_members("uni"),
            ("FOCUS", "HOT", "UNI"),
        )
        self.assertEqual(
            result.state.current_members("focus"),
            ("FOCUS", "HOT"),
        )
        self.assertEqual(result.state.current_members("hot"), ("HOT",))

    def setUp(self) -> None:
        self.events = (
            revision_event("uni", 0),
            revision_event("focus", 5),
            revision_event("hot", 10),
        )
        self.timeline = ReplayTimeline(
            session_date=SESSION_DATE,
            event_count=len(self.events),
            first_available_at_utc=self.events[0].available_at_utc,
            last_available_at_utc=self.events[-1].available_at_utc,
        )
        self.clock = FakeClock()
        self.controller = QuoteDashboardReplayController(
            timeline=self.timeline,
            event_factory=lambda: iter(self.events),
            speed=2,
            monotonic=self.clock.monotonic,
        )

    def test_play_pause_speed_and_step_follow_historical_time(self) -> None:
        initial = self.controller.snapshot()
        self.assertEqual(initial.status, DashboardReplayStatus.PAUSED)
        self.assertEqual(initial.applied_event_count, 0)
        self.assertEqual(initial.replay_time_utc, START)

        playing = self.controller.play()
        self.assertEqual(playing.status, DashboardReplayStatus.PLAYING)
        self.assertEqual(playing.applied_event_count, 1)

        self.clock.advance(2)
        before_focus = self.controller.tick()
        self.assertEqual(before_focus.replay_time_utc, START + timedelta(seconds=4))
        self.assertEqual(before_focus.applied_event_count, 1)

        self.controller.set_speed(10)
        self.clock.advance(0.2)
        after_focus = self.controller.tick()
        self.assertEqual(after_focus.replay_time_utc, START + timedelta(seconds=6))
        self.assertEqual(after_focus.applied_event_count, 2)

        paused = self.controller.pause()
        self.clock.advance(20)
        self.assertEqual(
            self.controller.tick().replay_time_utc,
            paused.replay_time_utc,
        )

        complete = self.controller.step()
        self.assertEqual(complete.status, DashboardReplayStatus.COMPLETE)
        self.assertEqual(complete.applied_event_count, 3)
        self.assertEqual(complete.progress_fraction, 1.0)

    def test_restart_and_finish_rebuild_state_from_the_source(self) -> None:
        self.controller.finish()
        restarted = self.controller.restart()

        self.assertEqual(restarted.status, DashboardReplayStatus.PAUSED)
        self.assertEqual(restarted.applied_event_count, 0)
        self.assertEqual(restarted.state.channels, ())

        finished = self.controller.finish()
        self.assertEqual(finished.status, DashboardReplayStatus.COMPLETE)
        self.assertEqual(finished.state.channels, ("focus", "hot", "uni"))

    def test_seek_reconstructs_the_inclusive_event_prefix(self) -> None:
        before_first = self.controller.seek(START - timedelta(microseconds=1))
        self.assertEqual(before_first.status, DashboardReplayStatus.PAUSED)
        self.assertEqual(before_first.applied_event_count, 0)
        self.assertEqual(
            before_first.replay_time_utc,
            START - timedelta(microseconds=1),
        )

        at_first = self.controller.seek(START)
        self.assertEqual(at_first.applied_event_count, 1)
        self.assertEqual(at_first.state.channels, ("uni",))

        between_events = self.controller.seek(START + timedelta(seconds=7))
        self.assertEqual(between_events.status, DashboardReplayStatus.PAUSED)
        self.assertEqual(between_events.applied_event_count, 2)
        self.assertEqual(
            between_events.replay_time_utc,
            START + timedelta(seconds=7),
        )
        self.assertEqual(between_events.state.channels, ("focus", "uni"))

        backward = self.controller.seek(START + timedelta(seconds=1))
        self.assertEqual(backward.applied_event_count, 1)
        self.assertEqual(backward.state.channels, ("uni",))

        repeated = self.controller.seek(START + timedelta(seconds=1))
        self.assertEqual(repeated, backward)

        after_last = self.controller.seek(START + timedelta(seconds=20))
        self.assertEqual(after_last.status, DashboardReplayStatus.COMPLETE)
        self.assertEqual(after_last.applied_event_count, 3)
        self.assertEqual(
            after_last.replay_time_utc,
            START + timedelta(seconds=20),
        )

    def test_seek_pauses_playback_and_preserves_speed(self) -> None:
        self.controller.play()
        self.controller.set_speed(10)

        sought = self.controller.seek(START + timedelta(seconds=7))

        self.assertEqual(sought.status, DashboardReplayStatus.PAUSED)
        self.assertEqual(sought.speed, 10)
        self.clock.advance(20)
        self.assertEqual(self.controller.tick(), sought)

    def test_seek_rejects_invalid_target_without_mutating_state(self) -> None:
        baseline = self.controller.seek(START + timedelta(seconds=7))

        with self.assertRaises(ValueError):
            self.controller.seek(datetime(2026, 9, 11, 9, 30))
        self.assertEqual(self.controller.snapshot(), baseline)

        with self.assertRaises(ValueError):
            self.controller.seek(START + timedelta(days=1))
        self.assertEqual(self.controller.snapshot(), baseline)

        with self.assertRaises(TypeError):
            self.controller.seek("09:30:00")  # type: ignore[arg-type]
        self.assertEqual(self.controller.snapshot(), baseline)

    def test_rejects_invalid_speed(self) -> None:
        with self.assertRaises(ValueError):
            self.controller.set_speed(0)
        with self.assertRaises(TypeError):
            self.controller.set_speed(True)


if __name__ == "__main__":
    unittest.main()
