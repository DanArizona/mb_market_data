from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from mb_market_data.observation_overlay import (
    ET as OVERLAY_ET,
    MembershipBand,
    ObservationOverlayOHLCVCache,
    ObservationOverlayProjector,
)
from mb_market_data.quote_dashboard_replay import (
    DashboardReplayStatus,
    QuoteDashboardReplayController,
)
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
            request_start_et=datetime(
                2026, 9, 11, 0, 0, tzinfo=OVERLAY_ET
            ),
            request_end_et=datetime(
                2026, 9, 11, 16, 5, tzinfo=OVERLAY_ET
            ),
            source_payload_sha256="a" * 64,
            candles=(),
        ),
        symbol="FOCUS",
        session_date=SESSION_DATE,
    )


class TestQuoteDashboardReplayController(unittest.TestCase):
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
        before_first = self.controller.seek(
            START - timedelta(microseconds=1)
        )
        self.assertEqual(before_first.status, DashboardReplayStatus.PAUSED)
        self.assertEqual(before_first.applied_event_count, 0)
        self.assertEqual(
            before_first.replay_time_utc,
            START - timedelta(microseconds=1),
        )

        at_first = self.controller.seek(START)
        self.assertEqual(at_first.applied_event_count, 1)
        self.assertEqual(at_first.state.channels, ("uni",))

        between_events = self.controller.seek(
            START + timedelta(seconds=7)
        )
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

        after_last = self.controller.seek(
            START + timedelta(seconds=20)
        )
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
