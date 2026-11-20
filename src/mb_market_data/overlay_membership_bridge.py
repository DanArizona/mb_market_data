"""Synthetic-only membership writer bridge for the experimental Overlay.

Historical replay has no path to this adapter. The adapter refuses any session
not explicitly marked as a disposable, offline synthetic demonstration.
OperatorState provides the existing authoritative preview/confirm rules; this
module neither writes a journal nor talks to Schwab.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .daily_membership import read, report_result
from .daily_operator_ui import OperatorProblem, OperatorState, membership_snapshot


class SyntheticOverlayMembership:
    """Expose the existing operator workflow to a synthetic Overlay preview."""

    def __init__(self, state_root: Path, *, clock: Callable | None = None):
        root = Path(state_root).resolve()
        plan = read(root / "plan.json")
        if plan.get("operator_demo_synthetic") is not True:
            raise ValueError("Overlay manual controls require a synthetic-only demo plan")
        if Path(plan.get("state_root", "")).resolve() != root:
            raise ValueError("Synthetic session identity does not match its state root")
        self.root = root
        self.operator = OperatorState(root, **({"clock": clock} if clock is not None else {}))

    @staticmethod
    def _require_mode(mode: str) -> None:
        if mode != "real-time":
            raise OperatorProblem("Historical and Backtest modes are read-only", 403)

    def snapshot(self, selected: str | None = None) -> dict[str, Any]:
        return membership_snapshot(self.root, selected=selected, clock=self.operator.clock)

    def records(self) -> dict[str, list[dict[str, Any]]]:
        """Exact exclusive roster; prices are fetched only for the inspector."""
        view = self.snapshot()
        return {
            tier: [
                {"symbol": symbol, "last_price": None,
                 "description": "SYNTHETIC · inspect symbol for fabricated quote"}
                for symbol in view["tiers"][tier]
            ]
            for tier in ("uni", "focus", "hot")
        }

    def preview(self, mode: str, symbol: str, action: str, target: str) -> dict:
        self._require_mode(mode)
        return self.operator.preview(symbol, action, target)

    def preview_drop(self, mode: str, symbol: str, source_tier: str,
                     destination_tier: str) -> dict:
        """Validate a proposed grid-to-grid drag; publication is never implicit.

        The source tier comes from an untrusted client event. Compare it with
        the effective journal membership before creating the existing
        operator's advisory Preview. That operator rechecks the revision
        again on explicit Confirm.
        """
        self._require_mode(mode)
        order = ("uni", "focus", "hot")
        if (source_tier not in order or destination_tier not in order
                or source_tier == destination_tier):
            raise OperatorProblem("Drop must move one symbol between different tiers.", 400)
        selected = self.snapshot(symbol).get("selected")
        if not selected or selected["tier"] != source_tier:
            raise OperatorProblem("Source tier changed or is unavailable. Refresh and drag again.")
        action = ("promote" if order.index(destination_tier) > order.index(source_tier)
                  else "demote")
        return self.preview(mode, symbol, action, destination_tier)

    def confirm(self, mode: str, proposal_id: str) -> dict:
        self._require_mode(mode)
        return self.operator.confirm(proposal_id)

    def result(self, command_id: str) -> dict:
        return report_result(self.root, command_id, clock=self.operator.clock)
