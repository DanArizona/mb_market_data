"""Read-only evidence audit. Reports acquisition times and recovery provenance."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mb_market_data.candle_session import CandleSessionReader
from mb_market_data.candle_supervision import instant


def audit(directory):
    reader = CandleSessionReader(directory)
    while not reader.caught_up:
        reader.refresh()
    fresh = CandleSessionReader(directory)
    while not fresh.caught_up:
        fresh.refresh()
    times = reader.availability_times
    for cutoff in times:
        if reader.project(cutoff) != fresh.project(cutoff):
            raise ValueError("Fresh reconstruction differs at an acquisition cutoff")
    events = [json.loads(p.read_text(encoding="utf-8")) for p in (Path(directory)/"health_events").glob("*.json")]
    for event in events:
        instant(event["observed_at_utc"])
        if event["kind"] == "INPUT_RESTORED":
            instant(event["recovery_acquisition_at_utc"])
    return {"result":"PASS", "symbol":reader.symbol,"session_date":str(reader.session_date),
        "committed_responses":reader.receipt_count,"cutoffs_reconstructed":len(times),
        "first_acquisition_at_utc":times[0].isoformat() if times else None,
        "last_acquisition_at_utc":times[-1].isoformat() if times else None,
        "input_interruptions":sum(e["kind"]=="INPUT_INTERRUPTED" for e in events),
        "restored_input_flows":sum(e["kind"]=="INPUT_RESTORED" for e in events),
        "backfill_status":"NEEDS_REVIEW" if any(e["kind"]=="INPUT_RESTORED" for e in events) else "NO_RECORDED_RECOVERY",
        "interpretation":"Hash/timing/reconstruction audit; does not assert all market intervals were backfilled or provider-finalized"}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session",type=Path)
    args=parser.parse_args()
    try:
        print(json.dumps(audit(args.session),indent=2))
        return 0
    except Exception as exc:
        print(f"Candle evidence audit FAILED: {type(exc).__name__}: {exc}",file=sys.stderr)
        return 1


if __name__=="__main__":
    raise SystemExit(main())
