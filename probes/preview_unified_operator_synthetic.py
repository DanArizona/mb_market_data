"""Launch unified Membership operator UI against a disposable synthetic session.

NO SCHWAB / NO LIVE CONTROLLER / NO PRODUCTION JOURNAL / NO CREDENTIALS.
The same established daily Membership worker processes temporary demo requests.
All files are created in an OS-managed temporary directory and discarded after
the user selects Stop server. RealTimeMembershipOperator (production) refuses
synthetic sessions; this launcher passes OperatorState only for a known demo.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mb_market_data.daily_membership import read, report_result
from mb_market_data.daily_operator_demo import DemoSession
from mb_market_data.daily_operator_ui import OperatorState
from mb_market_data.local_dashboard_server import LocalDashboardServer
from mb_market_data.observation_overlay import ObservationOverlayProjector
from mb_market_data.overlay_indexed_selection import IndexedOverlaySymbolSelection
from mb_market_data.quote_dashboard import create_quote_dashboard
from mb_market_data.quote_dashboard_replay import QuoteDashboardReplayController
from mb_market_data.quote_journal_replay import (
    MembershipRevisionEvent,
    QuoteJournalReplayReader,
)


class SyntheticOperatorActions:
    """Only an explicit disposable synthetic plan can supply this backend."""

    def __init__(self, root: Path) -> None:
        plan = read(root / "plan.json")
        if (
            plan.get("operator_demo_synthetic") is not True
            or Path(plan.get("state_root", "")).resolve() != root.resolve()
        ):
            raise ValueError("Unified synthetic viewer requires a disposable demo plan")
        self.root = root
        self.operator = OperatorState(root)

    def preview(self, symbol: str, action: str, target: str) -> dict:
        return self.operator.preview(symbol, action, target)

    def confirm(self, proposal_id: str) -> dict:
        return self.operator.confirm(proposal_id)

    def result(self, command_id: str) -> dict:
        return report_result(self.root, command_id, clock=self.operator.clock)


class SyntheticCommittedRefresh:
    """Follow only newly effective synthetic revisions; no real provider source."""

    def __init__(
        self, reader: QuoteJournalReplayReader,
        controller: QuoteDashboardReplayController,
        indexed: IndexedOverlaySymbolSelection,
    ) -> None:
        self.reader, self.controller, self.indexed = reader, controller, indexed
        self.controller.activate_live_readonly()

    def __call__(self) -> tuple[int, str]:
        now = datetime.now(timezone.utc)
        applied = self.controller.applied_event_ids()
        new = tuple(
            MembershipRevisionEvent(revision)
            for revision in self.reader.store.membership_revisions_in_effective_order()
            if revision.effective_at <= now
            and f"membership_revision:{revision.session_date.isoformat()}:r{revision.revision}"
            not in applied
        )
        if new:
            self.controller.append_live_committed(new)
            self.indexed.invalidate()
        latest = self.controller.snapshot()
        revision = max(
            (item.revision for item in
             self.reader.store.membership_revisions_in_effective_order()
             if item.effective_at <= now),
            default=None,
        )
        return len(new), (
            "OFFLINE SYNTHETIC · disposable journal / worker · "
            f"{len(new)} newly effective revision(s) · "
            f"current membership r{revision if revision is not None else 'none'} · "
            f"applied {latest.applied_event_count} synthetic events · "
            "NO SCHWAB OR PRODUCTION ACCESS"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8062)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("Choose an isolated local port from 1024 through 65535")

    with TemporaryDirectory(prefix="mb_UNIFIED_OPERATOR_SYNTHETIC_") as directory:
        demo = DemoSession(Path(directory))
        stop = Event()
        try:
            demo.start()
            reader = QuoteJournalReplayReader(Path(demo.plan["journal"]))
            def make_projector(symbol: str) -> ObservationOverlayProjector:
                return ObservationOverlayProjector(
                    cache=None, symbol=symbol,
                    session_date=reader.session_date,
                )
            indexed = IndexedOverlaySymbolSelection(reader, make_projector)
            controller = QuoteDashboardReplayController(
                timeline=reader.timeline(),
                event_factory=reader.events,
                overlay_projector_factory=lambda: make_projector("INTC"),
                symbol_projector_factory=make_projector,
                indexed_symbol_projector_factory=indexed,
            )
            controller.finish()
            refresh = SyntheticCommittedRefresh(reader, controller, indexed)
            app = create_quote_dashboard(
                controller,
                request_server_stop=stop.set,
                live_mode=True,
                live_refresh=refresh,
                operator_actions=SyntheticOperatorActions(demo.root),
                synthetic_operator_demo=True,
            )
            server = LocalDashboardServer(
                app.server, host="127.0.0.1",
                port=args.port, stop_event=stop,
            )
            print("OFFLINE SYNTHETIC UNIFIED OVERLAY — NO SCHWAB / NO PRODUCTION FILES", flush=True)
            print(f"Temporary synthetic journal: {demo.plan['journal']}", flush=True)
            print(f"Open Firefox: http://127.0.0.1:{args.port}/", flush=True)
            print("Membership: INTC Hot → Focus (Ordinary demote), Preview then Confirm.", flush=True)
            print("Wait for receipt/effective revision; then Promote INTC to Hot.", flush=True)
            print("Use Stop server in Firefox to end and clean up the demo.", flush=True)
            server.serve_until_stopped()
        finally:
            demo.close()
    print("Synthetic demo closed and temporary journal deleted.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
