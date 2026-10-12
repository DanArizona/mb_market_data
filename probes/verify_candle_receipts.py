"""Verify saved candle receipts and inclusive receipt-time visibility, read-only."""
from __future__ import annotations

import argparse
from datetime import timedelta
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from mb_market_data.candle_receipts import CandleReceiptReader


def verify(directory):
    reader = CandleReceiptReader(directory)
    while not reader.caught_up:
        reader.refresh(limit=32)
    times = reader.availability_times
    if not times:
        raise ValueError("No captured receipts")
    prior_closed, corrections, unfinished, snapshots = {}, set(), 0, 0
    for cutoff in times:
        before = reader.project(cutoff-timedelta(microseconds=1))
        after = reader.project(cutoff)
        if after.receipt_count <= before.receipt_count:
            raise ValueError("Receipt is not visible at its inclusive boundary")
        for version in after.closed:
            if not version.candle.available_at_utc <= version.received_at_utc <= cutoff:
                raise ValueError("Closed candle has invalid causal visibility")
            old = prior_closed.get(version.candle.start_et)
            if old and old.candle != version.candle:
                corrections.add(version.candle.start_et)
            prior_closed[version.candle.start_et] = version
        for version in after.pending:
            if version.received_at_utc > cutoff or version.interval_closed_at_receipt:
                raise ValueError("Pending candle has invalid receipt timing")
        unfinished += len(after.pending)
        snapshots += 1
    # A newly constructed process must reconstruct the identical cutoff state.
    fresh = CandleReceiptReader(directory)
    while not fresh.caught_up:
        fresh.refresh(limit=7)
    for cutoff in times:
        if fresh.project(cutoff) != reader.project(cutoff):
            raise ValueError("Fresh-process reconstruction differs")
    last = reader.project(times[-1])
    return {"symbol":reader.symbol,"session_date":str(reader.session_date),
            "receipts":last.receipt_count,"error_receipts":last.error_count,
            "receipt_cutoffs_checked":snapshots,"closed_candles_at_last_cutoff":len(last.closed),
            "unfinished_candles_at_last_cutoff":len(last.pending),
            "unfinished_observations_across_snapshots":unfinished,
            "candles_revised_after_closed_receipt":len(corrections),
            "fresh_process_reconstruction":"PASS","receipt_visibility":"PASS"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.directory)
    except Exception as exc:
        print(f"Candle receipt verification STOPPED: {type(exc).__name__}: {exc}")
        return 1
    print("Saved candle receipt verification: PASS")
    for name,value in result.items(): print(f"{name}: {value}")
    print("Original capture and production journals were not modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
