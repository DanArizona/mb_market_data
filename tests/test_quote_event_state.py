from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from mb_market_data.quote_event_state import (
    QuoteEventStateProjector,
    StateProjectionError,
)
from mb_market_data.quote_dashboard_view import build_quote_dashboard_view
from mb_market_data.quote_journal_replay import (
    ChannelRevisionEvent,
    QuoteAcquisitionEvent,
)
from mb_market_data.quote_observation_store import (
    SamplingChannelRevision,
    StoredAcquisition,
    StoredQuoteObservation,
)


UTC = timezone.utc
SESSION_DATE = date(2026, 9, 11)
START = datetime(2026, 9, 11, 13, 30, tzinfo=UTC)


def revision_event(
    channel: str,
    revision: int,
    symbols: tuple[str, ...],
    *,
    available_at: datetime = START,
) -> ChannelRevisionEvent:
    return ChannelRevisionEvent(
        SamplingChannelRevision(
            channel=channel,
            session_date=SESSION_DATE,
            revision=revision,
            effective_at=available_at,
            symbols=symbols,
            source="unit-test",
        )
    )


def acquisition_event(
    channel: str,
    revision: int,
    symbols: tuple[str, ...],
    *,
    acquisition_id: str,
    completed_at: datetime,
    price_start: float = 100.0,
) -> QuoteAcquisitionEvent:
    scheduled_at = completed_at - timedelta(milliseconds=500)
    acquisition = StoredAcquisition(
        acquisition_id=acquisition_id,
        run_id=f"run-{channel}",
        channel=channel,
        channel_revision=revision,
        session_date=SESSION_DATE,
        slot_id=f"slot-{acquisition_id}",
        scheduled_at_utc=scheduled_at,
        dispatched_at_utc=scheduled_at,
        completed_at_utc=completed_at,
        request_count=1,
        batch_size=400,
        requested_symbol_count=len(symbols),
        unexpected_symbols=(),
        content_sha256=f"sha256-{acquisition_id}",
    )
    observations = tuple(
        StoredQuoteObservation(
            acquisition_id=acquisition_id,
            channel=channel,
            channel_revision=revision,
            scheduled_at_utc=scheduled_at,
            symbol=symbol,
            ordinal=ordinal,
            status="quote",
            detail=None,
            schwab_batch_number=1,
            request_started_at_utc=scheduled_at,
            response_received_at_utc=completed_at,
            values={
                "quote_last_price": price_start + ordinal,
                "quote_total_volume": 1_000 + ordinal,
                "exchange": "Q",
                "description": f"{symbol} description",
            },
        )
        for ordinal, symbol in enumerate(symbols)
    )
    return QuoteAcquisitionEvent(acquisition, observations)


class TestQuoteEventStateProjector(unittest.TestCase):
    def test_preserves_overlapping_channel_membership_and_latest_quotes(
        self,
    ) -> None:
        projector = QuoteEventStateProjector(session_date=SESSION_DATE)
        projector.apply(revision_event("uni", 7, ("AAPL", "IPO")))
        projector.apply(revision_event("focus", 20, ("AAPL",)))
        projector.apply(
            acquisition_event(
                "uni",
                7,
                ("AAPL", "IPO"),
                acquisition_id="uni-1",
                completed_at=START + timedelta(seconds=1),
            )
        )
        projector.apply(
            acquisition_event(
                "focus",
                20,
                ("AAPL",),
                acquisition_id="focus-1",
                completed_at=START + timedelta(seconds=2),
            )
        )

        state = projector.snapshot()

        self.assertEqual(state.channels, ("focus", "uni"))
        self.assertEqual(state.current_members("UNI"), ("AAPL", "IPO"))
        self.assertEqual(state.memberships_for("aapl"), ("focus", "uni"))
        self.assertEqual(state.memberships_for("IPO"), ("uni",))
        self.assertEqual(
            state.latest("uni", "AAPL").acquisition.acquisition_id,
            "uni-1",
        )
        self.assertEqual(
            state.latest("focus", "AAPL").acquisition.acquisition_id,
            "focus-1",
        )
        self.assertEqual(
            tuple(row.symbol for row in state.channel_rows("uni")),
            ("AAPL", "IPO"),
        )
        self.assertTrue(
            all(row.latest is not None for row in state.channel_rows("uni"))
        )
        self.assertEqual(state.event_count, 4)
        self.assertEqual(state.revision_count, 2)
        self.assertEqual(state.acquisition_count, 2)
        self.assertEqual(state.observation_count, 3)

    def test_promotion_creates_new_focus_revision_without_changing_uni(
        self,
    ) -> None:
        projector = QuoteEventStateProjector()
        projector.apply(revision_event("uni", 7, ("AAPL", "IPO")))
        projector.apply(revision_event("focus", 20, ("AAPL",)))
        projector.apply(
            revision_event(
                "focus",
                21,
                ("AAPL", "IPO"),
                available_at=START + timedelta(minutes=1),
            )
        )

        state = projector.snapshot()

        self.assertEqual(state.current_members("uni"), ("AAPL", "IPO"))
        self.assertEqual(state.current_members("focus"), ("AAPL", "IPO"))
        self.assertEqual(state.channel_revisions["uni"].revision, 7)
        self.assertEqual(state.channel_revisions["focus"].revision, 21)

    def test_old_in_flight_acquisition_does_not_roll_back_membership(
        self,
    ) -> None:
        projector = QuoteEventStateProjector()
        projector.apply(revision_event("focus", 0, ("AAPL",)))
        projector.apply(
            revision_event(
                "focus",
                1,
                ("AAPL", "IPO"),
                available_at=START + timedelta(seconds=1),
            )
        )
        projector.apply(
            acquisition_event(
                "focus",
                0,
                ("AAPL",),
                acquisition_id="slow-old-request",
                completed_at=START + timedelta(seconds=2),
            )
        )

        state = projector.snapshot()

        self.assertEqual(state.channel_revisions["focus"].revision, 1)
        self.assertEqual(state.current_members("focus"), ("AAPL", "IPO"))
        self.assertIsNotNone(state.latest("focus", "AAPL"))
        self.assertIsNone(state.latest("focus", "IPO"))

    def test_removed_member_latest_is_retained_but_not_current(self) -> None:
        projector = QuoteEventStateProjector()
        projector.apply(revision_event("focus", 0, ("AAPL", "OLD")))
        projector.apply(
            acquisition_event(
                "focus",
                0,
                ("AAPL", "OLD"),
                acquisition_id="focus-before-removal",
                completed_at=START + timedelta(seconds=1),
            )
        )
        projector.apply(
            revision_event(
                "focus",
                1,
                ("AAPL",),
                available_at=START + timedelta(seconds=2),
            )
        )

        state = projector.snapshot()

        self.assertEqual(state.current_members("focus"), ("AAPL",))
        self.assertEqual(state.memberships_for("OLD"), ())
        self.assertIsNotNone(state.latest("focus", "OLD"))
        self.assertEqual(
            tuple(row.symbol for row in state.channel_rows("focus")),
            ("AAPL",),
        )

    def test_identical_event_redelivery_is_idempotent(self) -> None:
        projector = QuoteEventStateProjector()
        event = revision_event("focus", 0, ("SPY",))

        self.assertTrue(projector.apply(event))
        self.assertFalse(projector.apply(event))

        state = projector.snapshot()
        self.assertEqual(state.event_count, 1)
        self.assertEqual(state.revision_count, 1)

    def test_conflicting_event_id_is_rejected(self) -> None:
        projector = QuoteEventStateProjector()
        projector.apply(revision_event("focus", 0, ("SPY",)))

        with self.assertRaisesRegex(
            StateProjectionError,
            "conflicting content",
        ):
            projector.apply(revision_event("focus", 0, ("QQQ",)))

    def test_acquisition_requires_known_exact_revision(self) -> None:
        projector = QuoteEventStateProjector(session_date=SESSION_DATE)
        event = acquisition_event(
            "focus",
            99,
            ("SPY",),
            acquisition_id="unknown-revision",
            completed_at=START + timedelta(seconds=1),
        )

        with self.assertRaisesRegex(
            StateProjectionError,
            "unapplied channel revision",
        ):
            projector.apply(event)

    def test_acquisition_symbols_must_match_recorded_revision(self) -> None:
        projector = QuoteEventStateProjector()
        projector.apply(revision_event("focus", 0, ("SPY", "QQQ")))
        event = acquisition_event(
            "focus",
            0,
            ("SPY", "NVDA"),
            acquisition_id="wrong-symbols",
            completed_at=START + timedelta(seconds=1),
        )

        with self.assertRaisesRegex(
            StateProjectionError,
            "symbols differ",
        ):
            projector.apply(event)

    def test_observation_provenance_must_match_acquisition(self) -> None:
        projector = QuoteEventStateProjector()
        projector.apply(revision_event("focus", 0, ("SPY",)))
        event = acquisition_event(
            "focus",
            0,
            ("SPY",),
            acquisition_id="wrong-provenance",
            completed_at=START + timedelta(seconds=1),
        )
        changed_observation = replace(event.observations[0], channel="uni")
        changed_event = replace(
            event,
            observations=(changed_observation,),
        )

        with self.assertRaisesRegex(
            StateProjectionError,
            "provenance differs",
        ):
            projector.apply(changed_event)

    def test_rejects_wrong_session_or_time_order(self) -> None:
        projector = QuoteEventStateProjector(session_date=SESSION_DATE)
        first = revision_event("focus", 0, ("SPY",))
        projector.apply(first)

        wrong_session = ChannelRevisionEvent(
            replace(
                first.revision,
                session_date=date(2026, 9, 12),
                revision=1,
                effective_at=START + timedelta(seconds=1),
            )
        )
        with self.assertRaisesRegex(StateProjectionError, "belongs to"):
            projector.apply(wrong_session)

        older = revision_event(
            "uni",
            0,
            ("SPY",),
            available_at=START - timedelta(seconds=1),
        )
        with self.assertRaisesRegex(StateProjectionError, "precedes"):
            projector.apply(older)

    def test_snapshot_is_isolated_and_mappings_are_read_only(self) -> None:
        projector = QuoteEventStateProjector()
        projector.apply(revision_event("focus", 0, ("SPY",)))
        before = projector.snapshot()
        projector.apply(
            revision_event(
                "focus",
                1,
                ("SPY", "QQQ"),
                available_at=START + timedelta(seconds=1),
            )
        )

        self.assertEqual(before.current_members("focus"), ("SPY",))
        with self.assertRaises(TypeError):
            before.channel_revisions["focus"] = before.channel_revisions[
                "focus"
            ]


class TestQuoteDashboardView(unittest.TestCase):
    def test_collapses_current_membership_to_one_row_per_symbol(self) -> None:
        projector = QuoteEventStateProjector(session_date=SESSION_DATE)
        projector.apply(revision_event("uni", 7, ("AAPL", "IPO")))
        projector.apply(revision_event("focus", 20, ("AAPL", "SPY")))
        projector.apply(
            acquisition_event(
                "uni",
                7,
                ("AAPL", "IPO"),
                acquisition_id="uni-view",
                completed_at=START + timedelta(seconds=1),
                price_start=100.0,
            )
        )
        projector.apply(
            acquisition_event(
                "focus",
                20,
                ("AAPL", "SPY"),
                acquisition_id="focus-view",
                completed_at=START + timedelta(seconds=2),
                price_start=250.0,
            )
        )

        view = build_quote_dashboard_view(projector.snapshot())
        records = {row["symbol"]: row for row in view.grid_records()}

        self.assertEqual(view.session_date, SESSION_DATE)
        self.assertEqual(view.unique_member_count, 3)
        self.assertEqual(view.multi_channel_count, 1)
        self.assertEqual(tuple(records), ("AAPL", "IPO", "SPY"))
        self.assertEqual(records["AAPL"]["channels"], "focus, uni")
        self.assertEqual(records["AAPL"]["uni_revision"], "r7")
        self.assertEqual(records["AAPL"]["focus_revision"], "r20")
        self.assertIsNone(records["AAPL"]["hot_revision"])
        self.assertEqual(records["AAPL"]["latest_channel"], "focus")
        self.assertEqual(records["AAPL"]["last_price"], 250.0)
        self.assertEqual(records["IPO"]["channels"], "uni")
        self.assertEqual(records["SPY"]["channels"], "focus")
        self.assertEqual(
            tuple(summary.channel for summary in view.channels),
            ("focus", "uni"),
        )
        self.assertEqual(view.channels[0].status_counts, {"quote": 2})

    def test_empty_state_produces_an_empty_dashboard(self) -> None:
        view = build_quote_dashboard_view(
            QuoteEventStateProjector().snapshot()
        )

        self.assertIsNone(view.session_date)
        self.assertEqual(view.as_of_et_text, "No events")
        self.assertEqual(view.channels, ())
        self.assertEqual(view.grid_records(), [])
