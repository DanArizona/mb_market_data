from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from mb_market_data.sampling_membership import (
    SAMPLING_HIERARCHY_CONTRACT,
    MembershipRevisionConflictError,
    MembershipRevisionError,
    RevisionTransition,
    SamplingChannel,
    SamplingHierarchyRevision,
    assess_revision_candidate,
)


UTC = timezone.utc
SESSION_DATE = date(2026, 9, 16)
EFFECTIVE_AT = datetime(2026, 9, 16, 13, 30, tzinfo=UTC)
PUBLISHED_AT = EFFECTIVE_AT - timedelta(minutes=30)


def make_revision(
    *,
    revision: int = 0,
    effective_at: datetime = EFFECTIVE_AT,
    published_at: datetime | None = PUBLISHED_AT,
    session_date: date = SESSION_DATE,
    uni_symbols=("AAPL", "NVDA", "SPY"),
    focus_symbols=("AAPL", "NVDA"),
    hot_symbols=("NVDA",),
    source: str = "unit-test",
    reason: str | None = "initial membership",
    metadata=None,
    contract: str = SAMPLING_HIERARCHY_CONTRACT,
) -> SamplingHierarchyRevision:
    return SamplingHierarchyRevision(
        session_date=session_date,
        revision=revision,
        effective_at=effective_at,
        uni_symbols=uni_symbols,
        focus_symbols=focus_symbols,
        hot_symbols=hot_symbols,
        source=source,
        reason=reason,
        metadata={} if metadata is None else metadata,
        published_at=published_at,
        contract=contract,
    )


class TestSamplingHierarchyRevision(unittest.TestCase):

    def test_normalizes_deduplicates_and_canonically_orders_sets(
        self,
    ) -> None:
        revision = make_revision(
            uni_symbols=(" spy ", "NVDA", "aapl", "SPY"),
            focus_symbols=("nvda", " AAPL ", "NVDA"),
            hot_symbols=(" nvda ",),
        )

        self.assertEqual(revision.uni_symbols, ("AAPL", "NVDA", "SPY"))
        self.assertEqual(revision.focus_symbols, ("AAPL", "NVDA"))
        self.assertEqual(revision.hot_symbols, ("NVDA",))

    def test_allows_empty_focus_and_hot(self) -> None:
        revision = make_revision(focus_symbols=(), hot_symbols=())

        self.assertEqual(revision.focus_symbols, ())
        self.assertEqual(revision.hot_symbols, ())

    def test_rejects_empty_uni(self) -> None:
        with self.assertRaisesRegex(ValueError, "uni_symbols.*at least one"):
            make_revision(
                uni_symbols=(),
                focus_symbols=(),
                hot_symbols=(),
            )

    def test_rejects_focus_outside_uni(self) -> None:
        with self.assertRaisesRegex(
            MembershipRevisionError,
            "Focus must be a subset of Uni.*TSLA",
        ):
            make_revision(focus_symbols=("AAPL", "TSLA"))

    def test_rejects_hot_outside_focus(self) -> None:
        with self.assertRaisesRegex(
            MembershipRevisionError,
            "Hot must be a subset of Focus.*SPY",
        ):
            make_revision(hot_symbols=("NVDA", "SPY"))

    def test_canonical_hash_ignores_input_order_and_timezone_spelling(
        self,
    ) -> None:
        first = make_revision(
            uni_symbols=("SPY", "NVDA", "AAPL"),
            focus_symbols=("NVDA", "AAPL"),
            metadata={"producer": "test", "limits": {"count": 3}},
        )
        second = make_revision(
            effective_at=EFFECTIVE_AT.astimezone(
                timezone(timedelta(hours=-4))
            ),
            uni_symbols=("AAPL", "SPY", "NVDA", "AAPL"),
            focus_symbols=("AAPL", "NVDA"),
            metadata={"limits": {"count": 3}, "producer": "test"},
        )

        self.assertEqual(first.content_sha256, second.content_sha256)
        self.assertEqual(len(first.content_sha256), 64)

    def test_hash_includes_membership_and_provenance(self) -> None:
        original = make_revision()
        changed_membership = make_revision(
            uni_symbols=("AAPL", "NVDA", "SPY", "TSLA")
        )
        changed_reason = make_revision(reason="manual correction")

        self.assertNotEqual(
            original.content_sha256,
            changed_membership.content_sha256,
        )
        self.assertNotEqual(
            original.content_sha256,
            changed_reason.content_sha256,
        )

    def test_hash_excludes_store_assigned_publication_time(self) -> None:
        first = make_revision(published_at=PUBLISHED_AT)
        second = make_revision(
            published_at=PUBLISHED_AT + timedelta(minutes=5)
        )

        self.assertEqual(first.content_sha256, second.content_sha256)

    def test_metadata_is_copied_and_deeply_read_only(self) -> None:
        metadata = {"producer": {"names": ["OV", "LUDP"]}}
        revision = make_revision(metadata=metadata)
        metadata["producer"]["names"].append("manual")

        self.assertEqual(
            revision.metadata["producer"]["names"],
            ("OV", "LUDP"),
        )
        with self.assertRaises(TypeError):
            revision.metadata["producer"] = {}
        with self.assertRaises(TypeError):
            revision.metadata["producer"]["new"] = True

    def test_rejects_invalid_identity_and_publication_values(self) -> None:
        with self.assertRaisesRegex(TypeError, "revision must be an integer"):
            make_revision(revision=True)
        with self.assertRaisesRegex(ValueError, "source.*nonblank"):
            make_revision(source=" ")
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            make_revision(effective_at=EFFECTIVE_AT.replace(tzinfo=None))
        with self.assertRaisesRegex(
            MembershipRevisionError,
            "effective_at cannot precede published_at",
        ):
            make_revision(published_at=EFFECTIVE_AT + timedelta(seconds=1))
        with self.assertRaisesRegex(
            MembershipRevisionError,
            "unsupported sampling hierarchy contract",
        ):
            make_revision(contract="future-contract")

    def test_rejects_non_json_metadata(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON-compatible"):
            make_revision(metadata={"bad": object()})
        with self.assertRaisesRegex(ValueError, "finite"):
            make_revision(metadata={"bad": float("nan")})

    def test_returns_membership_by_channel(self) -> None:
        revision = make_revision()

        self.assertEqual(
            revision.symbols_for(SamplingChannel.UNI),
            revision.uni_symbols,
        )
        self.assertEqual(
            revision.symbols_for(" FOCUS "),
            revision.focus_symbols,
        )
        self.assertEqual(revision.symbols_for("hot"), revision.hot_symbols)
        with self.assertRaisesRegex(ValueError, "unknown sampling channel"):
            revision.symbols_for("blocked")


class TestRevisionTransition(unittest.TestCase):

    def test_initial_revision_must_be_r0(self) -> None:
        self.assertEqual(
            assess_revision_candidate(None, make_revision()),
            RevisionTransition.INITIAL,
        )

        with self.assertRaisesRegex(ValueError, "initial.*r0"):
            assess_revision_candidate(None, make_revision(revision=1))

    def test_identical_revision_is_idempotent(self) -> None:
        current = make_revision()
        retry = make_revision(
            published_at=PUBLISHED_AT + timedelta(minutes=1),
            uni_symbols=("SPY", "AAPL", "NVDA"),
        )

        self.assertEqual(
            assess_revision_candidate(current, retry),
            RevisionTransition.ALREADY_PRESENT,
        )

    def test_same_revision_with_different_content_conflicts(self) -> None:
        with self.assertRaises(MembershipRevisionConflictError):
            assess_revision_candidate(
                make_revision(),
                make_revision(reason="different content"),
            )

    def test_accepts_exact_sequential_revision(self) -> None:
        current = make_revision()
        candidate = make_revision(
            revision=1,
            effective_at=EFFECTIVE_AT + timedelta(minutes=5),
            published_at=EFFECTIVE_AT + timedelta(minutes=1),
            focus_symbols=("AAPL",),
            hot_symbols=(),
            reason="demote NVDA",
        )

        self.assertEqual(
            assess_revision_candidate(current, candidate),
            RevisionTransition.NEXT,
        )

    def test_rejects_no_op_next_revision(self) -> None:
        current = make_revision()
        candidate = make_revision(
            revision=1,
            published_at=PUBLISHED_AT + timedelta(minutes=1),
        )

        with self.assertRaisesRegex(
            MembershipRevisionError,
            "does not change membership or retained provenance",
        ):
            assess_revision_candidate(current, candidate)

    def test_rejects_stale_gap_and_wrong_session_revisions(self) -> None:
        current = make_revision(
            revision=2,
            effective_at=EFFECTIVE_AT + timedelta(minutes=10),
            published_at=EFFECTIVE_AT + timedelta(minutes=1),
        )

        with self.assertRaisesRegex(ValueError, "stale.*r1"):
            assess_revision_candidate(
                current,
                make_revision(revision=1),
            )
        with self.assertRaisesRegex(ValueError, "gap.*r3.*r4"):
            assess_revision_candidate(
                current,
                make_revision(
                    revision=4,
                    effective_at=EFFECTIVE_AT + timedelta(minutes=20),
                    published_at=EFFECTIVE_AT + timedelta(minutes=2),
                ),
            )
        with self.assertRaisesRegex(ValueError, "session dates"):
            assess_revision_candidate(
                current,
                make_revision(
                    revision=3,
                    effective_at=EFFECTIVE_AT + timedelta(days=1),
                    published_at=EFFECTIVE_AT + timedelta(minutes=2),
                    session_date=SESSION_DATE + timedelta(days=1),
                ),
            )

    def test_rejects_time_regressions(self) -> None:
        current = make_revision(
            effective_at=EFFECTIVE_AT + timedelta(minutes=10),
            published_at=EFFECTIVE_AT,
        )

        with self.assertRaisesRegex(ValueError, "candidate effective_at"):
            assess_revision_candidate(
                current,
                make_revision(
                    revision=1,
                    effective_at=EFFECTIVE_AT + timedelta(minutes=5),
                    published_at=EFFECTIVE_AT + timedelta(minutes=1),
                ),
            )

        current_with_later_publication = make_revision(
            published_at=EFFECTIVE_AT - timedelta(minutes=5)
        )
        with self.assertRaisesRegex(ValueError, "candidate published_at"):
            assess_revision_candidate(
                current_with_later_publication,
                make_revision(
                    revision=1,
                    effective_at=EFFECTIVE_AT + timedelta(minutes=1),
                    published_at=EFFECTIVE_AT - timedelta(minutes=10),
                ),
            )


if __name__ == "__main__":
    unittest.main()
