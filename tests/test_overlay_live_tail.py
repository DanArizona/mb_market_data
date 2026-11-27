"""Read-only committed event tail checks; no network, provider or prod files."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mb_market_data.observation_overlay import ObservationOverlayProjector, MembershipBand
from mb_market_data.overlay_live_tail import ReadOnlyJournalTail
from mb_market_data.quote_dashboard import create_quote_dashboard
from mb_market_data.quote_dashboard_replay import QuoteDashboardReplayController
from mb_market_data.quote_journal_replay import MembershipRevisionEvent, ReplayTimeline
from mb_market_data.quote_observation_store import StoredAcquisition, StoredQuoteObservation
from mb_market_data.sampling_membership import SamplingHierarchyRevision

DAY = date(2026, 11, 24)
UTC = timezone.utc
START = datetime(2026, 11, 24, 14, 30, tzinfo=UTC)


def revision(number: int, second: int, *, hot: bool = False) -> MembershipRevisionEvent:
    return MembershipRevisionEvent(SamplingHierarchyRevision(
        session_date=DAY, revision=number,
        effective_at=START + timedelta(seconds=second),
        uni_symbols=("AAA",), focus_symbols=("AAA",),
        hot_symbols=("AAA",) if hot else (),
        source="offline-test",
    ))


def acquisition(second: int) -> tuple[StoredAcquisition, StoredQuoteObservation]:
    t = START + timedelta(seconds=second)
    a = StoredAcquisition(
        acquisition_id=f"focus:{second}", run_id="run",
        channel="focus", channel_revision=0, session_date=DAY,
        slot_id=f"focus:{second}",
        scheduled_at_utc=t - timedelta(seconds=1),
        dispatched_at_utc=t - timedelta(seconds=1),
        completed_at_utc=t, request_count=1, batch_size=400,
        requested_symbol_count=1, unexpected_symbols=(), content_sha256="a"*64,
    )
    o = StoredQuoteObservation(
        acquisition_id=a.acquisition_id, channel="focus", channel_revision=0,
        scheduled_at_utc=a.scheduled_at_utc, symbol="AAA", ordinal=0,
        status="quote", detail=None, schwab_batch_number=1,
        request_started_at_utc=a.dispatched_at_utc,
        response_received_at_utc=a.completed_at_utc,
        values={"quote_last_price": 42.5, "quote_total_volume": 1200},
    )
    return a, o


class FakeReader:
    def __init__(self, headers, observations) -> None:
        self.session_date = DAY
        self.database_path = Path("/tmp/fake-2026-11-24.sqlite3")
        self.headers = list(headers)
        self.store = SimpleNamespace(
            get_observations=lambda acquisition_id: observations[acquisition_id]
        )

    def _ordered_headers(self):
        return tuple(self.headers)


class FakeObserver:
    def __init__(self, _root, _date) -> None:
        self._session = SimpleNamespace(
            journal_path=str(Path("/tmp/fake-2026-11-24.sqlite3").resolve())
        )
        self.count = 0

    def observe(self, *, now):
        return SimpleNamespace(
            committed_acquisitions=self.count,
            latest_acquisition_completed_at_utc=now.isoformat(),
            controller_phase_saved="COLLECTING_DEGRADED",
        )


class TestReadOnlyJournalTail(unittest.TestCase):
    def test_preopen_empty_dashboard_keeps_chart_and_renders_later_committed_quote(self):
        """Starting before r0 is effective must not permanently omit Charts."""
        for initial_headers in ((), (revision(0, 0),)):
            with self.subTest(future_r0_already_saved=bool(initial_headers)):
                a1, o1 = acquisition(5)
                reader = FakeReader(initial_headers, {a1.acquisition_id: (o1,)})
                first = START if initial_headers else None
                controller = QuoteDashboardReplayController(
                    timeline=ReplayTimeline(DAY, len(initial_headers), first, first),
                    event_factory=lambda: iter(initial_headers),
                    overlay_projector_factory=lambda: ObservationOverlayProjector(
                        cache=None, symbol="AAA", session_date=DAY,
                    ),
                )
                clock = [START - timedelta(minutes=1)]
                controller.seek(clock[0])
                with patch("mb_market_data.overlay_live_tail.ReadOnlySessionObserver", FakeObserver):
                    tail = ReadOnlyJournalTail(
                        data_root=Path("/tmp"), reader=reader, controller=controller,
                    )
                app = create_quote_dashboard(
                    controller, live_mode=True,
                    live_refresh=lambda: tail.refresh(now=clock[0]),
                )
                client = app.server.test_client()
                with client.get("/_dash-layout") as layout:
                    self.assertEqual(layout.status_code, 200)
                    self.assertTrue(
                        b'"id":"observation-overlay-chart"' in layout.data,
                        "Preopen startup omitted the chart from the permanent layout",
                    )
                    self.assertNotIn(b"No Observation Overlay evidence", layout.data)

                def callback_for(output_id):
                    return next(
                        (key, callback) for key, callback in app.callback_map.items()
                        if isinstance(callback["output"], list)
                        and any(item.component_id == output_id for item in callback["output"])
                    )

                chart_key, chart_callback = callback_for("observation-overlay-chart")

                def post_callback(key, callback, trigger, values):
                    with client.post("/_dash-update-component", json={
                        "output": key,
                        "outputs": [
                            {"id": item.component_id, "property": item.component_property}
                            for item in callback["output"]
                        ],
                        "changedPropIds": [trigger],
                        **{
                            group: [dict(item, value=values.get(item["id"])) for item in callback[group]]
                            for group in ("inputs", "state")
                        },
                    }) as response:
                        self.assertEqual(response.status_code, 200)
                        return response.get_json()["response"]

                if not initial_headers:
                    reader.headers.append(revision(0, 0))
                self.assertEqual(tail.refresh(now=clock[0])[0], 0)
                empty = controller.snapshot()
                self.assertEqual(empty.applied_event_count, 0)
                self.assertIsNone(empty.state.current_time_utc)
                self.assertIsNone(empty.last_event_time_utc)
                self.assertEqual(empty.observation_overlay.replay_time_utc, clock[0])
                self.assertEqual(empty.observation_overlay.membership_transitions, ())
                self.assertEqual(empty.observation_overlay.quote_points, ())
                self.assertEqual(empty.observation_overlay.candles, ())

                reader.headers.append(a1)
                clock[0] = START + timedelta(seconds=7)
                live_key, live_callback = callback_for("live-data-status")
                live = post_callback(
                    live_key, live_callback, "live-tail-tick.n_intervals",
                    {"live-tail-tick": 1},
                )
                self.assertEqual(live["live-refresh-count"]["data"]["event_count"], 2)
                chart = post_callback(
                    chart_key, chart_callback, "live-refresh-count.data",
                    {"live-refresh-count": live["live-refresh-count"]["data"],
                     "membership-selected-symbol": "AAA", "observation-overlay-units": "price"},
                )
                self.assertEqual(chart["observation-overlay-band"]["children"], "Focus")
                quote = next(
                    trace for trace in chart["observation-overlay-chart"]["figure"]["data"]
                    if trace["name"] == "Focus quote"
                )
                self.assertEqual(quote["y"], [42.5])
                self.assertEqual(controller.snapshot().state.acquisition_count, 1)
                self.assertEqual(controller.snapshot().observation_overlay.candles, ())

    def build(self):
        r0 = revision(0, 0)
        a1, o1 = acquisition(5)
        reader = FakeReader((r0,), {a1.acquisition_id: (o1,)})
        controller = QuoteDashboardReplayController(
            timeline=ReplayTimeline(DAY, 1, r0.available_at_utc, r0.available_at_utc),
            event_factory=lambda: iter((r0,)),
            overlay_projector_factory=lambda: ObservationOverlayProjector(
                cache=None, symbol="AAA", session_date=DAY,
            ),
        )
        controller.finish()
        patcher = patch("mb_market_data.overlay_live_tail.ReadOnlySessionObserver", FakeObserver)
        patcher.start()
        self.addCleanup(patcher.stop)
        tail = ReadOnlyJournalTail(
            data_root=Path("/tmp"), reader=reader, controller=controller
        )
        return tail, reader, controller, a1

    def test_new_committed_acquisition_updates_market_and_overlay_without_repeat(self):
        tail, reader, controller, a1 = self.build()
        initial = controller.snapshot()
        reader.headers.append(a1)
        new, status = tail.refresh(now=START + timedelta(seconds=7))
        self.assertEqual(new, 1)
        self.assertEqual(controller.snapshot().state.acquisition_count, 1)
        self.assertEqual(controller.snapshot().applied_event_count, initial.applied_event_count + 1)
        self.assertEqual(len(controller.snapshot().observation_overlay.quote_points), 1)
        self.assertEqual(controller.snapshot().observation_overlay.quote_points[0].values["quote_last_price"], 42.5)
        self.assertIn("liveness not verified", status)
        again, _ = tail.refresh(now=START + timedelta(seconds=9))
        self.assertEqual(again, 0)
        self.assertEqual(controller.snapshot().state.acquisition_count, 1)

    def test_future_membership_revision_not_displayed_until_effective(self):
        tail, reader, controller, _ = self.build()
        r1 = revision(1, 20, hot=True)
        reader.headers.append(r1)
        early, _ = tail.refresh(now=START + timedelta(seconds=10))
        self.assertEqual(early, 0)
        self.assertEqual(controller.snapshot().observation_overlay.current_band, MembershipBand.FOCUS)
        later, _ = tail.refresh(now=START + timedelta(seconds=21))
        self.assertEqual(later, 1)
        self.assertEqual(controller.snapshot().observation_overlay.current_band, MembershipBand.HOT)
        self.assertEqual(controller.snapshot().state.channel_revisions["hot"].revision, 1)

    def test_readonly_live_mode_refuses_historical_reset_and_duplicate_event(self):
        tail, reader, controller, a1 = self.build()
        with self.assertRaisesRegex(RuntimeError, "reset"):
            controller.restart()
        reader.headers.append(a1)
        tail.refresh(now=START + timedelta(seconds=7))
        from mb_market_data.quote_journal_replay import QuoteAcquisitionEvent
        observation = reader.store.get_observations(a1.acquisition_id)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            controller.append_live_committed((QuoteAcquisitionEvent(a1, observation),))


if __name__ == "__main__":
    unittest.main()
