"""One attended, single-Schwab-request current-session OHLCV experiment.

Run from the separate experimental checkout, never from an installed daily
controller. This probe does not spawn a second daily session, modify a quote
journal, publish membership or alter any production source. It uses the same
authenticated configuration and serialized *client initialization* as today's
production controller; a refresh may legitimately update the shared OAuth
token store. A single price_history call captures completed-only five-minute
candles for one symbol into a separate immutable evidence directory.

No unattended retry, periodic polling or browser OAuth authorization.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import getpass
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mb_market_data.daily_runtime import owner_available, prepared_client
from mb_market_data.observation_overlay import ET
from mb_market_data.overlay_live_ohlcv import (
    _symbol, acquire_live_ohlcv, publish_live_ohlcv,
)
from mb_market_data.overlay_session_catalog import inspect_session

CONTRACT = "one-unlock-daily-flow-v1"


def _fresh(text: str, *, now: datetime, label: str) -> None:
    """Require recent timestamped evidence instead of trusting a saved PID."""
    if not isinstance(text, str) or not text:
        raise ValueError(f"Missing {label} timestamp")
    observed = datetime.fromisoformat(text)
    if observed.utcoffset() is None:
        raise ValueError(f"Naive {label} timestamp")
    age = now.astimezone(timezone.utc) - observed.astimezone(timezone.utc)
    if not timedelta(seconds=-3) <= age <= timedelta(seconds=120):
        raise ValueError(f"Stale or future {label} timestamp")


def verify_collecting_channels(status: dict, *, now: datetime) -> None:
    """Allow historical Hot gaps, but require actively healthy workers now."""
    phase = status.get("phase")
    if phase not in {"COLLECTING", "COLLECTING_DEGRADED"}:
        raise ValueError(f"Controller is not collecting ({phase})")
    health = status.get("health")
    if not isinstance(health, dict) or health.get("journal_read") == "UNAVAILABLE" or health.get("error_type"):
        raise ValueError("Journal health is unavailable")
    channels, workers = health.get("channels"), status.get("workers")
    if not isinstance(channels, dict) or not isinstance(workers, dict):
        raise ValueError("Channel/worker health evidence missing")
    expected = ("uni", "focus", "hot") if status.get("hot_activated") else ("uni", "focus")
    for name in expected:
        worker, channel = workers.get(name), channels.get(name)
        if not isinstance(worker, dict) or not isinstance(channel, dict):
            raise ValueError(f"{name} worker/channel missing")
        if worker.get("exit_code") is not None or worker.get("api_state") != "AVAILABLE":
            raise ValueError(f"{name} worker exited or API unavailable")
        if channel.get("state") != "RUNNING":
            raise ValueError(f"{name} not currently running")
        count = channel.get("committed_acquisitions")
        if type(count) is not int or count < 1:
            raise ValueError(f"{name} has no committed acquisitions")
        outcomes = channel.get("latest_outcomes")
        if not isinstance(outcomes, dict) or outcomes.get("request_error", 0):
            raise ValueError(f"{name} recent request error")
        _fresh(worker.get("api_at"), now=now, label=f"{name} API")
        _fresh(channel.get("last_committed_slot_utc"), now=now, label=f"{name} acquisition")


def load_live_plan(plan_file: Path, *, now: datetime) -> dict:
    """Strict read-only current-session, active-owner preflight."""
    plan_path = plan_file.expanduser().resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(plan, dict) or plan.get("contract") != CONTRACT:
        raise ValueError("Not an accepted one-unlock daily session plan")
    day = date.fromisoformat(plan["session_date"])
    current = now.astimezone(ET)
    if day != current.date():
        raise ValueError("Refusing another ET date's production session")
    data_root = Path(plan["data_root"]).resolve()
    state_root = Path(plan["state_root"]).resolve()
    journal = Path(plan["journal"]).resolve()
    expected_state = data_root / "output" / "daily_session" / day.isoformat()
    if state_root != expected_state or plan_path != state_root / "plan.json":
        raise ValueError("Session plan/state paths do not match the exact ET day")
    if (
        journal != data_root / "output"
        / f"quote_observation_journal_v2_api_opening_{day}"
        / f"{day}.sqlite3"
    ):
        raise ValueError("Production quote journal path does not match plan")
    session = inspect_session(data_root, day)
    if Path(session.journal_path).resolve() != journal:
        raise ValueError("Journal identity cannot be verified")
    opening = datetime.fromisoformat(plan["opens_at"]).astimezone(ET)
    close = datetime.fromisoformat(plan["closes_at"]).astimezone(ET)
    if opening.date() != day or close.date() != day:
        raise ValueError("Exchange open/close do not match requested ET date")
    if not opening + timedelta(minutes=5) < current < close:
        raise ValueError("One-shot trial requires an open session with completed candles")
    status = json.loads((state_root / "status.json").read_text(encoding="utf-8"))
    owner = json.loads((state_root / "owner.json").read_text(encoding="utf-8"))
    if (
        status.get("session_date") != day.isoformat()
        or type(status.get("pid")) is not int
        or type(owner.get("pid")) is not int
        or status["pid"] != owner["pid"]
    ):
        raise ValueError("Daily controller status/owner identity mismatch")
    verify_collecting_channels(status, now=current)
    if not owner_available(state_root, pid=status["pid"]):
        raise ValueError("The production controller owner heartbeat is not current")
    timestamp = datetime.fromisoformat(status["at"]).astimezone(ET)
    if not timedelta(seconds=-3) <= current - timestamp <= timedelta(seconds=15):
        raise ValueError("Saved controller status is stale or from the future")
    if (state_root / "stop.request").exists() or (state_root / "exit.request").exists():
        raise ValueError("The production controller is stopping; abort the OHLCV test")
    if not Path(plan["ecfg"]).is_file():
        raise ValueError("Encrypted configuration from daily plan is unavailable")
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-file", required=True, type=Path)
    parser.add_argument("--symbol", default="MSFT")
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument(
        "--live", action="store_true",
        help="Explicitly authorize exactly one price_history call",
    )
    args = parser.parse_args(argv)
    try:
        symbol = _symbol(args.symbol)
        plan = load_live_plan(args.plan_file, now=datetime.now(ET))
        output_root = args.output_root.expanduser().resolve()
        data_root = Path(plan["data_root"]).resolve()
        if output_root == data_root or output_root.is_relative_to(data_root):
            raise ValueError("Trial evidence must be outside the production data root")
        if importlib.util.find_spec("mb_tools.schwab_secure") is None:
            raise RuntimeError("Active Conda environment does not provide mb_tools")
    except Exception as error:
        print(f"LIVE OHLCV PREFLIGHT FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        return 2

    print("ONE-SHOT REAL SCHWAB OHLCV QUALIFICATION")
    print(f"Symbol         : {symbol}")
    print(f"Session        : {plan['session_date']}")
    print("Price history  : exactly ONE 5-minute request; completed bars only")
    print(f"Evidence root  : {output_root}")
    print("Production     : controller/journal/membership unchanged by this probe")
    print("Authentication : shared token store; noninteractive renewal only")
    if not args.live:
        print("PREFLIGHT PASS. No provider call; include --live to authorize one request.")
        return 0

    # The only interactive step. Never echo, save or log the password.
    password = getpass.getpass("Encrypted config password (one-shot OHLCV): ")
    client = None
    request_attempted = False
    try:
        # Preexisting daily authorization code coordinates SDK client setup
        # with the production workers. OAuth browser reauthorization is blocked.
        client = prepared_client(plan, password)
        password = None
        # An attended password entry may have taken time. Revalidate the live
        # daily owner BEFORE making the separate one-shot SDK request.
        plan = load_live_plan(args.plan_file, now=datetime.now(ET))
        print("Authorization verified; sending ONE Schwab price-history request...", flush=True)
        request_attempted = True
        acquisition = acquire_live_ohlcv(
            client,
            symbol=symbol,
            session_date=date.fromisoformat(plan["session_date"]),
            session_end_et=datetime.fromisoformat(plan["closes_at"]),
        )
        output = publish_live_ohlcv(output_root, acquisition)
    except Exception as error:
        # SDK exceptions can contain provider URLs. Do not print their
        # messages, HTTP bodies, credentials or token information.
        print(
            f"LIVE OHLCV FAILED: {type(error).__name__}; "
            f"price-history request {'attempted' if request_attempted else 'not attempted'}. "
            "No repeat request is made.",
            file=sys.stderr, flush=True,
        )
        return 1
    finally:
        password = None
        if client is not None:
            try:
                client.close()
            except Exception:
                print("WARNING: close() raised; no additional request attempted", file=sys.stderr)

    candles = acquisition.cache.candles
    print("LIVE OHLCV: PASS (one actual Schwab response, saved)")
    print(f"HTTP status          : {acquisition.http_status}")
    print(f"Completed bars       : {len(candles)}")
    print(f"Provisional excluded : {acquisition.skipped_provisional_candles}")
    print(f"First candle ET      : {candles[0].start_et.isoformat()}")
    print(f"Latest candle ET     : {candles[-1].start_et.isoformat()}")
    print(f"Latest candle volume : {candles[-1].volume:,}")
    print(f"Raw response SHA-256 : {acquisition.cache.source_payload_sha256}")
    print(f"Bundle directory     : {output}")
    print(f"Cache file           : {output / 'ohlcv_cache.json'}")
    print("No production quote journal or membership writer used.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
