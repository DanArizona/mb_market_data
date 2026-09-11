"""Audit one daily SQLite quote-observation journal."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mb_market_data.quote_journal_audit import audit_quote_journal


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate structure, configured-slot completeness, provenance, "
            "result counts, and storage size for a daily quote journal."
        )
    )
    parser.add_argument("database", type=Path, help="Daily .sqlite3 file")
    parser.add_argument(
        "--show-missing",
        type=int,
        default=10,
        metavar="N",
        help="Show at most N missing slot IDs per channel (default: 10)",
    )
    args = parser.parse_args()
    if args.show_missing < 0:
        parser.error("--show-missing must be nonnegative")
    return args


def human_bytes(value: int) -> str:
    amount = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if amount < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} B"
            return f"{amount:.1f} {unit}"
        amount /= 1024.0
    raise AssertionError("unreachable")


def main() -> int:
    args = parse_args()
    try:
        report = audit_quote_journal(args.database)
    except Exception as exc:
        print(f"Journal audit ERROR: {type(exc).__name__}: {exc}")
        return 2

    print()
    print(f"Quote journal audit: {'PASS' if report.passed else 'FAIL'}")
    print("=" * 79)
    print(f"Database         : {report.database_path}")
    print(f"Session date     : {report.session_date}")
    print(f"Schema version   : {report.schema_version}")
    print(f"SQLite integrity : {', '.join(report.integrity_results)}")
    print()
    print("Table rows")
    for name, count in report.table_counts.items():
        print(f"  {name:<29} {count:>12,}")

    print()
    print("Polling runs")
    for run in report.runs:
        print(
            f"  {run.channel or 'unknown':<8} {run.started_at_utc}  "
            f"{run.software_version}  {run.host or '-'}"
        )

    print()
    print("Channels")
    for channel in report.channels:
        statuses = ", ".join(
            f"{name}={count:,}"
            for name, count in channel.status_counts.items()
        ) or "none"
        print(f"  {channel.channel}")
        print(
            f"    revisions={channel.revision_count:,}  "
            f"member rows={channel.member_rows:,}"
        )
        print(
            f"    acquisitions={channel.acquisition_count:,}/"
            f"{channel.expected_slot_count:,} expected  "
            f"missing={len(channel.missing_slot_ids):,}"
        )
        print(
            f"    observations={channel.observation_count:,}/"
            f"{channel.requested_symbol_count:,} requested  "
            f"row mismatches={len(channel.row_count_mismatch_ids):,}"
        )
        print(f"    statuses: {statuses}")
        for slot_id in channel.missing_slot_ids[: args.show_missing]:
            print(f"    missing: {slot_id}")
        remaining = len(channel.missing_slot_ids) - args.show_missing
        if remaining > 0:
            print(f"    ... {remaining:,} more missing slots")

    print()
    print("Storage")
    storage_total = sum(report.storage_bytes.values())
    for name, size in report.storage_bytes.items():
        print(f"  {name:<12} {human_bytes(size):>12}")
    print(f"  {'SQLite total':<12} {human_bytes(storage_total):>12}")
    if report.evidence_bytes:
        evidence_total = sum(report.evidence_bytes.values())
        print(f"  {'evidence':<12} {human_bytes(evidence_total):>12}")

    if report.findings:
        print()
        print("Findings")
        for finding in report.findings:
            print(f"  [{finding.code}] {finding.detail}")

    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
