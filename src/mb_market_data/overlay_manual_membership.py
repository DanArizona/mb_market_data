"""Explicit opt-in Real Time Membership adapter; no second hierarchy writer.

The Dashboard may preview proposals through the previously tested OperatorState.
An explicit operator Confirm queues *only* a request with daily_membership.submit;
the existing membership worker remains the sole publisher of atomic revisions.
Historical mode never constructs this adapter. The normal Real Time viewer leaves
it disabled until an attended, separately authorized operator launch.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from .daily_membership import CONTRACT, normalized, read, report_result
from .daily_operator_ui import OperatorProblem, OperatorState, membership_snapshot
from .overlay_session_catalog import inspect_session


def preview_validated_drop(
    state: OperatorState,
    root: Path,
    symbol: str,
    source_tier: str,
    destination_tier: str,
) -> dict:
    """Check actual effective source, then preview; a drop NEVER submits.

    Source/target/symbol are untrusted browser inputs. The authoritative
    OperatorState.preview rechecks owner heartbeat, capacity, effective
    revision and the 90-second deadline before any later Confirm.
    """
    tiers = ("uni", "focus", "hot")
    if (
        source_tier not in tiers
        or destination_tier not in tiers
        or source_tier == destination_tier
    ):
        raise OperatorProblem("Drop must move one symbol between different tiers.", 400)
    exact_symbol = normalized(symbol)
    record = membership_snapshot(root, selected=exact_symbol, clock=state.clock)
    effective = record.get("selected")
    if not isinstance(effective, dict) or effective.get("tier") != source_tier:
        raise OperatorProblem("Source tier changed or is unavailable. Refresh and retry.")
    action = (
        "promote"
        if tiers.index(destination_tier) > tiers.index(source_tier)
        else "demote"
    )
    return state.preview(exact_symbol, action, destination_tier)


class RealTimeMembershipOperator:
    """Bind the existing operator preview/confirm logic to one validated session."""

    def __init__(
        self,
        data_root: str | Path,
        session_date: str | date,
        *,
        operator_state: OperatorState | None = None,
    ) -> None:
        session = inspect_session(data_root, session_date)
        root = Path(session.plan_path).parent.resolve()
        plan = read(root / "plan.json")
        if (
            plan.get("membership_control") != CONTRACT
            or plan.get("operator_demo_synthetic")
        ):
            raise ValueError(
                "Controlled manual actions require a real daily session "
                "explicitly launched with membership controls"
            )
        if (
            Path(plan["state_root"]).resolve() != root
            or Path(plan["journal"]).resolve() != Path(session.journal_path).resolve()
        ):
            raise ValueError("Operator plan and selected journal identity differ")
        self.root = root
        self.session_date = session.session_date
        self._state = operator_state or OperatorState(root)

    def preview(self, symbol: str, action: str, target: str) -> dict:
        """Advisory, nonmutating preview, with existing owner/worker gates."""
        return self._state.preview(normalized(symbol), action, target)

    def preview_drop(
        self, symbol: str, source_tier: str, destination_tier: str,
    ) -> dict:
        return preview_validated_drop(
            self._state, self.root, symbol, source_tier, destination_tier,
        )

    def confirm(self, proposal_id: str) -> dict:
        """Explicitly queue via existing submit; never publish the journal."""
        return self._state.confirm(proposal_id)

    def result(self, command_id: str) -> dict:
        """Read the authoritative worker receipt or pending request state."""
        return report_result(self.root, command_id, clock=self._state.clock)
