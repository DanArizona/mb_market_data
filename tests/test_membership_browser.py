from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from types import MappingProxyType, SimpleNamespace

from mb_market_data.membership_browser import (
    describe_membership_symbol,
    highest_tier,
    membership_tier_records,
    selected_symbol_details,
)
from mb_market_data.quote_dashboard_view import DashboardSymbolRow, QuoteDashboardView


def member(
    symbol: str,
    channels: tuple[str, ...],
    values: dict[str, object] | None = None,
) -> DashboardSymbolRow:
    now = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)
    latest = (
        SimpleNamespace(
            observation=SimpleNamespace(status="quote", values=values),
            acquisition=SimpleNamespace(channel=channels[-1], completed_at_utc=now),
        )
        if values is not None
        else None
    )
    return DashboardSymbolRow(
        symbol=symbol,
        channels=channels,
        channel_revisions=MappingProxyType({channel: 3 for channel in channels}),
        latest=latest,
    )


def view_of(*rows: DashboardSymbolRow) -> QuoteDashboardView:
    return QuoteDashboardView(
        session_date=date(2026, 11, 13),
        as_of_utc=datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc),
        event_count=12,
        acquisition_count=10,
        observation_count=10,
        unique_member_count=len(rows),
        multi_channel_count=sum(len(row.channels) > 1 for row in rows),
        channels=(),
        rows=tuple(rows),
    )


class TestMembershipBrowser(unittest.TestCase):
    def test_exclusive_display_keeps_nested_underlying_membership(self) -> None:
        view = view_of(
            member("AAA", ("uni",)),
            member("BBB", ("focus", "uni")),
            member("CCC", ("focus", "hot", "uni")),
        )
        lists = membership_tier_records(view)
        self.assertEqual(
            {tier: [record["symbol"] for record in records] for tier, records in lists.items()},
            {"uni": ["AAA"], "focus": ["BBB"], "hot": ["CCC"]},
        )
        self.assertEqual(selected_symbol_details(view, "ccc").channels, ("focus", "hot", "uni"))

    def test_quote_fields_are_as_of_observation_and_market_cap_is_estimated(self) -> None:
        row = member(
            "ONFO", ("focus", "hot", "uni"),
            {
                "description": "Onfolio Holdings Inc.",
                "quote_last_price": 0.75,
                "quote_close_price": 0.6,
                "quote_total_volume": 123_000,
                "quote_open_price": 0.72,
                "quote_high_price": 0.91,
                "quote_low_price": 0.64,
                "shares_outstanding": 4_000_000,
                "quote_security_status": "Halted",
            },
        )
        details = describe_membership_symbol(row)
        self.assertEqual(details.highest_tier, "hot")
        self.assertAlmostEqual(details.change_from_close_percent, 25.0)
        self.assertEqual(details.estimated_market_cap, 3_000_000)
        self.assertEqual(details.volume, 123_000)
        self.assertEqual(details.security_status, "Halted")
        self.assertEqual(details.observed_at_et, "2026-11-13T10:00:00.000-05:00")

    def test_missing_quote_does_not_invent_price_or_blockage(self) -> None:
        details = describe_membership_symbol(member("SPY", ("uni",)))
        self.assertIsNone(details.last_price)
        self.assertIsNone(details.estimated_market_cap)
        self.assertIsNone(details.previous_close)
        self.assertEqual(details.observation_status, "not_observed")

    def test_missing_or_stale_selected_member_is_not_carried_across_replay(self) -> None:
        before = view_of(member("AAA", ("uni",)))
        self.assertIsNone(selected_symbol_details(before, "ONFO"))
        after = view_of(member("ONFO", ("uni",)))
        self.assertIsNotNone(selected_symbol_details(after, "ONFO"))

    def test_invalid_numeric_values_cannot_produce_fictitious_metrics(self) -> None:
        details = describe_membership_symbol(member(
            "ZZZ", ("uni",),
            {"quote_last_price": float("nan"), "quote_close_price": 0,
             "shares_outstanding": True, "quote_total_volume": "unavailable"},
        ))
        self.assertIsNone(details.last_price)
        self.assertIsNone(details.change_from_close_percent)
        self.assertIsNone(details.estimated_market_cap)
        self.assertIsNone(details.volume)

    def test_missing_hierarchy_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "no Uni"):
            highest_tier(())


if __name__ == "__main__":
    unittest.main()
