"""Fail-closed next-session preparation check for production closeout.

READ-ONLY. No network, no credentials, no snapshot acquisition, no journal
modification, and no production process control. A PASS is only next-session
opening-r0 readiness; current-session audit remains a separate verdict.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mb_market_data.opening_hierarchy import load_opening_proposal
from mb_market_data.quote_journal_replay import QuoteJournalReplayReader
from mb_market_data.equity_closeout_calendar import validate_next_exchange_session


def _json(path: Path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def inspect_preparation(*, root: Path, closed_date: date, next_date: date,
                        journal_root: Path, workflow_dir: Path | None = None):
    """Return explicit readiness evidence or raise an actionable exception."""
    if next_date <= closed_date:
        raise ValueError("Next session must follow completed session")
    validate_next_exchange_session(closed_date, next_date)
    directory = workflow_dir or root / "output" / "daily_universe_production" / (
        f"{next_date.isoformat()}-from-{closed_date.isoformat()}")
    directory = Path(directory)
    manifest = _json(directory / "workflow_manifest.json")
    if (manifest.get("session_date") != closed_date.isoformat()
            or manifest.get("target_date") != next_date.isoformat()):
        raise ValueError("Workflow source/target dates differ; no silent carry-forward")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError("Workflow has no artifact inventory")
    required = {"decision_ledger", "market_snapshot_manifest",
                "opening_hierarchy_r0", "symbol_directory_manifest",
                "uni_symbols", "uni_watchlist", "universe_manifest"}
    if not required <= set(artifacts):
        raise ValueError("Workflow is missing mandatory artifacts")
    for name in sorted(required):
        entry = artifacts[name]
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError(f"Invalid artifact entry: {name}")
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Unsafe artifact path: {name}")
        actual = hashlib.sha256((directory / relative).read_bytes()).hexdigest()
        if actual != entry.get("sha256"):
            raise ValueError(f"Artifact hash differs: {name}")
    proposal_path = directory / artifacts["opening_hierarchy_r0"]["path"]
    proposal = load_opening_proposal(proposal_path)
    if (proposal.session_date != next_date or proposal.revision != 0
            or len(proposal.uni_symbols) == 0 or proposal.focus_symbols
            or proposal.hot_symbols):
        raise ValueError("Next-session opening r0 is not a valid Uni-only roster")
    if (proposal.metadata.get("universe_source_session_date")
            != closed_date.isoformat()):
        raise ValueError("Opening r0 does not cite completed-session source")
    journal_path = journal_root / f"{next_date.isoformat()}.sqlite3"
    reader = QuoteJournalReplayReader(journal_path)
    if reader.session_date != next_date or reader.schema_version != 2:
        raise ValueError("Next-session schema-v2 journal date/version differs")
    revisions = reader.store.membership_revisions_in_effective_order()
    recorded = next((r for r in revisions if r.revision == 0), None)
    if recorded is None or recorded.content_sha256 != proposal.content_sha256:
        raise ValueError("Opening r0 is not published with the proposal hash")
    if recorded.published_at > recorded.effective_at:
        raise ValueError("Opening r0 published after effective time")
    # A reader must be able to reconstruct all available events; do not mistake
    # a published r0 for a successful current-session acquisition audit.
    list(reader.events())
    return {
        "status": "READY",
        "closed_session": closed_date.isoformat(),
        "next_session": next_date.isoformat(),
        "source_session": closed_date.isoformat(),
        "workflow_manifest": str(directory / "workflow_manifest.json"),
        "opening_proposal": str(proposal_path),
        "journal": str(journal_path),
        "uni_members": len(proposal.uni_symbols),
        "r0_content_sha256": proposal.content_sha256,
        "workflow_artifacts_verified": len(required),
        "scope": "next-session source/proposal/publication readiness only; current-session audit is separate",
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--closed-session", type=date.fromisoformat, required=True)
    p.add_argument("--next-session", type=date.fromisoformat, required=True)
    p.add_argument("--market-data-root", type=Path, default=Path.cwd())
    p.add_argument("--journal-root", type=Path)
    p.add_argument("--workflow-dir", type=Path)
    args = p.parse_args()
    root = args.market_data_root.resolve()
    journal_root = args.journal_root or root / "output" / (
        f"quote_observation_journal_v2_api_opening_{args.next_session.isoformat()}")
    try:
        evidence = inspect_preparation(root=root, closed_date=args.closed_session,
            next_date=args.next_session, journal_root=journal_root,
            workflow_dir=args.workflow_dir)
    except Exception as exc:
        print("NEXT-SESSION PREPARATION: INCOMPLETE")
        print(f"Reason: {type(exc).__name__}: {exc}")
        print("Closeout must not be declared COMPLETE; preserve evidence and finish dated preparation.")
        return 2
    print("NEXT-SESSION PREPARATION: READY")
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
