"""Offline file-evolution and launch checks for daily Nasdaq chart evidence."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mb_market_data.observation_overlay import ObservationOverlayProjector
from mb_market_data.observation_overlay_view import resolve_daily_halt_markers
from mb_market_data.overlay_live_halts import LiveHaltEventsReader, live_halt_events_path
from mb_market_data.quote_dashboard_replay import QuoteDashboardReplayController
from mb_market_data.quote_journal_replay import (
    MembershipRevisionEvent, QuoteAcquisitionEvent, ReplayTimeline,
)
from mb_market_data.quote_observation_store import StoredAcquisition, StoredQuoteObservation
from mb_market_data.sampling_membership import SamplingHierarchyRevision

DAY = date(2026, 11, 27)
UTC = timezone.utc
START = datetime(2026, 11, 27, 14, 30, tzinfo=UTC)


def quote_event(second: int) -> QuoteAcquisitionEvent:
    at = START + timedelta(seconds=second)
    acquisition = StoredAcquisition(
        acquisition_id=f"hot:{second}", run_id="offline-run", channel="hot",
        channel_revision=0, session_date=DAY, slot_id=f"hot:{second}",
        scheduled_at_utc=at - timedelta(seconds=1),
        dispatched_at_utc=at - timedelta(seconds=1), completed_at_utc=at,
        request_count=1, batch_size=400, requested_symbol_count=2,
        unexpected_symbols=(), content_sha256="a" * 64,
    )
    observations = tuple(StoredQuoteObservation(
        acquisition_id=acquisition.acquisition_id, channel="hot", channel_revision=0,
        scheduled_at_utc=acquisition.scheduled_at_utc, symbol=symbol, ordinal=i,
        status="quote", detail=None, schwab_batch_number=1,
        request_started_at_utc=acquisition.dispatched_at_utc,
        response_received_at_utc=at,
        values={"quote_last_price": price, "quote_total_volume": 1200},
    ) for i, (symbol, price) in enumerate((("NVDA", 147.0), ("ZYN", 8.5))))
    return QuoteAcquisitionEvent(acquisition, observations)


def events():
    return (
        MembershipRevisionEvent(SamplingHierarchyRevision(
            session_date=DAY, revision=0, effective_at=START,
            uni_symbols=("NVDA", "ZYN"), focus_symbols=("NVDA", "ZYN"),
            hot_symbols=("NVDA", "ZYN"), source="offline-live-halt-test",
        )),
        quote_event(70), quote_event(600),
    )


def episode(symbol: str, minute: int, *, observed_minute: int | None = None):
    observed_minute = minute + 1 if observed_minute is None else observed_minute
    halted = START.replace(minute=minute)
    observed = START.replace(minute=observed_minute)
    return f"{symbol}@{halted.isoformat()}", {
        "first_observed_at": observed.isoformat(),
        "last_observed_at": observed.isoformat(),
        "record": {
            "symbol": symbol, "halt_date": "11/27/2026",
            "halt_time": f"09:{minute:02d}:00", "reason_code": "LUDP",
            "retrieval_mode": "CURRENT",
        },
    }


def publish(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(value), encoding="utf-8")
    pending.replace(path)


def controller_for(source: LiveHaltEventsReader) -> QuoteDashboardReplayController:
    replay_events = events()
    factory = lambda symbol: ObservationOverlayProjector(
        cache=None, symbol=symbol, session_date=DAY,
        halt_intervals=source.latest(symbol),
    )
    controller = QuoteDashboardReplayController(
        timeline=ReplayTimeline(DAY, len(replay_events), START, START + timedelta(minutes=10)),
        event_factory=lambda: iter(replay_events),
        overlay_projector_factory=lambda: factory("ZYN"),
        symbol_projector_factory=factory,
    )
    controller.finish()
    controller.activate_live_readonly()
    return controller


class TestLiveHaltEvents(unittest.TestCase):
    def test_exact_session_auto_attachment_and_explicit_sources(self):
        root = Path("controller-data")
        self.assertEqual(
            live_halt_events_path(root, session_date=DAY),
            root / "output" / "daily_session" / "2026-11-27" / "nasdaq" / "events.json",
        )
        explicit = Path("selected/events.json")
        self.assertEqual(live_halt_events_path(
            root, session_date=DAY, daily_events_path=explicit,
        ), explicit)
        self.assertIsNone(live_halt_events_path(
            root, session_date=DAY, historical_halts_path=Path("normalized.json"),
        ))
        self.assertIsNone(live_halt_events_path(
            None, session_date=DAY, daily_events_path=explicit,
        ))

    def test_file_evolution_updates_same_symbol_and_retains_invalid_source_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nasdaq" / "events.json"
            clock = [START + timedelta(minutes=10)]
            source = LiveHaltEventsReader(path, session_date=DAY, now_factory=lambda: clock[0])
            controller = controller_for(source)
            before_count = controller.snapshot().applied_event_count
            self.assertIn("WAITING", source.refresh(controller)[1])
            self.assertEqual(controller.snapshot().observation_overlay.halt_intervals, ())

            first_key, first = episode("ZYN", 31)
            saved = {first_key: first}
            publish(path, saved)
            self.assertEqual(source.refresh(controller)[0], 1)
            self.assertEqual(len(resolve_daily_halt_markers(
                controller.snapshot().observation_overlay, off_hours=False,
            )), 1)

            # The producer's alternating retrieval must preserve the episode.
            first["record"]["retrieval_mode"] = "HISTORICAL"
            first["last_observed_at"] = START.replace(minute=34).isoformat()
            second_key, second = episode("ZYN", 35)
            saved[second_key] = second
            future_key, future = episode("ZYN", 42)
            saved[future_key] = future
            publish(path, saved)
            changed, status = source.refresh(controller)
            self.assertEqual(changed, 1)
            self.assertNotIn("UNAVAILABLE", status)
            visible = controller.snapshot().observation_overlay
            self.assertEqual(len(visible.halt_intervals), 2)
            self.assertEqual(len(resolve_daily_halt_markers(visible, off_hours=False)), 2)
            self.assertEqual(source.refresh(controller)[0], 0)

            # A syntactically valid truncation is also rejected: known episodes
            # cannot silently disappear from either the chart or reader cache.
            controller.select_overlay_symbol("NVDA")
            publish(path, {})
            controller.select_overlay_symbol("ZYN")
            self.assertIn("UNAVAILABLE", source.status("ZYN"))
            self.assertEqual(controller.snapshot().observation_overlay.halt_intervals,
                             visible.halt_intervals)
            self.assertIn("UNAVAILABLE", source.refresh(controller)[1])
            self.assertEqual(controller.snapshot().observation_overlay.halt_intervals,
                             visible.halt_intervals)

            # An invalid replacement or a temporarily absent file leaves the
            # displayed valid episodes intact and says evidence is retained.
            path.write_text("{bad-json", encoding="utf-8")
            self.assertIn("UNAVAILABLE", source.refresh(controller)[1])
            self.assertIn("retaining", source.status("ZYN"))
            self.assertEqual(controller.snapshot().observation_overlay.halt_intervals,
                             visible.halt_intervals)
            path.unlink()
            self.assertIn("WAITING", source.refresh(controller)[1])
            self.assertEqual(controller.snapshot().observation_overlay.halt_intervals,
                             visible.halt_intervals)

            # The selected symbol never inherits another symbol's episodes.
            controller.select_overlay_symbol("NVDA")
            self.assertEqual(controller.snapshot().observation_overlay.halt_intervals, ())
            nvda_key, nvda = episode("NVDA", 33)
            saved[nvda_key] = nvda
            publish(path, saved)
            self.assertEqual(source.refresh(controller)[0], 1)
            self.assertEqual(len(controller.snapshot().observation_overlay.halt_intervals), 1)
            controller.select_overlay_symbol("ZYN")
            self.assertEqual(len(controller.snapshot().observation_overlay.halt_intervals), 2)
            self.assertEqual(controller.snapshot().applied_event_count, before_count)
            self.assertEqual(controller.snapshot().observation_overlay.candles, ())

            # A real receipt may advance the halt display beyond the most
            # recent quote, but a future receipt must wait for the live clock.
            # Neither case manufactures any new quote/journal event.
            clock[0] = START + timedelta(minutes=13)
            source.refresh(controller)
            self.assertEqual(len(controller.snapshot().observation_overlay.halt_intervals), 3)
            self.assertEqual(controller.snapshot().applied_event_count, before_count)
            self.assertEqual(controller.snapshot().observation_overlay.replay_time_utc, clock[0])

    def test_future_resumption_receipt_waits_for_clock_even_when_file_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.json"
            clock = [START + timedelta(minutes=10)]
            source = LiveHaltEventsReader(path, session_date=DAY, now_factory=lambda: clock[0])
            key, saved = episode("ZYN", 31)
            saved["last_observed_at"] = START.replace(minute=41).isoformat()
            saved["record"].update(
                resumption_date="11/27/2026", resumption_trade_time="09:39:00",
            )
            saved["resumption_history_complete"] = True
            saved["resumption_updates"] = [{
                "resumption_date": "11/27/2026", "resumption_trade_time": "09:39:00",
                "observed_at": START.replace(minute=41).isoformat(),
            }]
            publish(path, {key: saved})
            controller = controller_for(source)
            before_count = controller.snapshot().applied_event_count
            self.assertIsNone(controller.snapshot().observation_overlay.halt_intervals[0].end_et)
            self.assertEqual(source.refresh(controller)[0], 0)
            clock[0] = START.replace(minute=41)
            self.assertEqual(source.refresh(controller)[0], 1)
            interval = controller.snapshot().observation_overlay.halt_intervals[0]
            self.assertEqual(interval.end_et.strftime("%H:%M:%S"), "09:39:00")
            self.assertEqual(controller.snapshot().observation_overlay.replay_time_utc, clock[0])
            self.assertEqual(controller.snapshot().applied_event_count, before_count)

            # Older daily files may lack captures and explicit endpoint
            # history. A later last_observed fallback must preserve the
            # earlier endpoint receipt already known to this reader.
            saved.pop("resumption_updates")
            saved["resumption_history_complete"] = False
            saved["last_observed_at"] = START.replace(minute=42).isoformat()
            clock[0] = START.replace(minute=42)
            publish(path, {key: saved})
            self.assertEqual(source.refresh(controller)[0], 0)
            self.assertEqual(source.latest("ZYN")[0].end_observed_at_utc,
                             START.replace(minute=41))
            self.assertEqual(controller.snapshot().observation_overlay.halt_intervals[0], interval)

            # A stale but syntactically valid file cannot reopen an endpoint
            # that the live viewer has already received and displayed.
            saved["resumption_updates"] = []
            saved["record"].pop("resumption_trade_time")
            saved["record"].pop("resumption_date")
            controller.select_overlay_symbol("NVDA")
            publish(path, {key: saved})
            controller.select_overlay_symbol("ZYN")
            self.assertIn("UNAVAILABLE", source.status("ZYN"))
            self.assertEqual(controller.snapshot().observation_overlay.halt_intervals[0], interval)
            self.assertIn("UNAVAILABLE", source.refresh(controller)[1])
            self.assertEqual(controller.snapshot().observation_overlay.halt_intervals[0], interval)

    def test_probe_live_tick_attaches_halts_before_any_candle_is_available(self):
        probe_path = Path(__file__).resolve().parents[1] / "probes" / "dashboard_quote_observation_journal.py"
        spec = importlib.util.spec_from_file_location("offline_live_halt_probe", probe_path)
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)

        class Clock:
            @staticmethod
            def now(zone):
                return (START + timedelta(minutes=10)).astimezone(zone)

        class Tail:
            def __init__(self, **kwargs):
                kwargs["controller"].activate_live_readonly()

            def refresh(self):
                return 0, "Committed journal"

        for candles_enabled in (False, True):
            with self.subTest(candles_enabled=candles_enabled), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                database = root / "2026-11-27.sqlite3"
                args = SimpleNamespace(
                    database=database, real_time_data_root=root,
                    real_time_ohlcv_root=root / "candles" if candles_enabled else None,
                    overlay_symbol="ZYN", observation_overlay_cache=None,
                    nasdaq_halts_json=None, nasdaq_daily_events_json=None,
                    enable_ohlcv_selection=False, enable_manual_membership_actions=False,
                    start_at_beginning=False, polling_health_directory=None,
                    host="127.0.0.1", port=8051,
                )
                replay_events = events()
                reader = SimpleNamespace(
                    session_date=DAY, timeline=lambda: ReplayTimeline(
                        DAY, len(replay_events), START, START + timedelta(minutes=10),
                    ), events=lambda: iter(replay_events), poll_seconds_by_run=lambda: {},
                )
                captured = {}

                def dashboard(controller, **kwargs):
                    captured.update(controller=controller, **kwargs)
                    return SimpleNamespace(server=object())

                with (
                    patch.object(probe, "parse_args", return_value=args),
                    patch.object(probe, "datetime", Clock),
                    patch.object(probe, "QuoteJournalReplayReader", return_value=reader),
                    patch.object(probe, "IndexedOverlaySymbolSelection", return_value=None),
                    patch.object(probe, "inspect_session", return_value=SimpleNamespace(journal_path=str(database))),
                    patch.object(probe, "ReadOnlyJournalTail", Tail),
                    patch.object(probe, "LiveOHLCVReader", return_value=SimpleNamespace(latest=lambda symbol: None)),
                    patch.object(probe, "LiveHaltEventsReader", side_effect=lambda path, **kwargs: LiveHaltEventsReader(
                        path, **kwargs, now_factory=lambda: START + timedelta(minutes=10),
                    )),
                    patch.object(probe, "create_quote_dashboard", side_effect=dashboard),
                    patch.object(probe, "LocalDashboardServer", return_value=SimpleNamespace(serve_until_stopped=lambda: None)),
                    patch("builtins.print"),
                ):
                    self.assertEqual(probe.main(), 0)
                refresh = captured["live_refresh"]
                self.assertIn("WAITING", refresh()[1])
                path = live_halt_events_path(root, session_date=DAY)
                publish(path, dict((episode("ZYN", 31),)))
                changed, status = refresh()
                self.assertEqual(changed, 1)
                self.assertIn("1 saved episode", status)
                if candles_enabled:
                    self.assertIn("no independent completed-candle snapshot", status)
                overlay = captured["controller"].snapshot().observation_overlay
                self.assertEqual(len(resolve_daily_halt_markers(overlay, off_hours=False)), 1)
                self.assertEqual(overlay.candles, ())
