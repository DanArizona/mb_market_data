from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from mb_market_data.quote_dashboard_replay import (
    DashboardReplayStatus,
    QuoteDashboardReplayController,
)
from mb_market_data.quote_journal_replay import (
    ChannelRevisionEvent,
    ReplayTimeline,
)
from mb_market_data.quote_observation_store import SamplingChannelRevision


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


class TestQuoteDashboardReplayController(unittest.TestCase):
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

    def test_rejects_invalid_speed(self) -> None:
        with self.assertRaises(ValueError):
            self.controller.set_speed(0)
        with self.assertRaises(TypeError):
            self.controller.set_speed(True)


if __name__ == "__main__":
    unittest.main()
