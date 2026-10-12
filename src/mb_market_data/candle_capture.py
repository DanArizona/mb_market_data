"""Bounded, standalone live-candle evidence capture. No journal connection."""
from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from mb_market_data.candle_supervision import DirectoryLock, publish_json

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
FIELDS = ("open", "high", "low", "close", "volume")


class CaptureInputError(ValueError):
    """Safe local input message; never an authentication-library error."""


def stamp(value=None):
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="microseconds")


def bounds(session_date):
    return (datetime.combine(session_date, datetime.min.time(), ET).replace(hour=9, minute=30),
            datetime.combine(session_date, datetime.min.time(), ET).replace(hour=16))


def synthetic_bounds(session_date):
    start = datetime.combine(session_date, datetime.min.time(), ET)
    return start, start.replace(hour=23, minute=59, second=59)


def validate_window(session_date, now):
    start, end = bounds(session_date)
    if session_date != now.astimezone(ET).date() or not start <= now < end:
        raise CaptureInputError("Capture requires today's regular-session window, 09:30 <= ET < 16:00.")
    if session_date.weekday() >= 5:
        raise CaptureInputError("Weekend capture is refused.")
    return start, end


def decode_candles(body, session_date, *, synthetic=False):
    payload = json.loads(body)
    if not isinstance(payload, dict) or not isinstance(payload.get("candles"), list):
        raise ValueError("Response has no candle list")
    selected, seen = [], set()
    start, end = synthetic_bounds(session_date) if synthetic else bounds(session_date)
    for candle in payload["candles"]:
        if not isinstance(candle, dict):
            raise ValueError("Malformed candle")
        epoch = candle.get("datetime")
        if isinstance(epoch, bool) or not isinstance(epoch, (int, float)) or not math.isfinite(epoch):
            raise ValueError("Invalid candle timestamp")
        when = datetime.fromtimestamp(epoch / 1000, UTC)
        if not start <= when < end:
            continue
        if when.second or when.microsecond or when.minute % 5:
            raise ValueError("Candle timestamp is not on a five-minute boundary")
        if when in seen:
            raise ValueError("Duplicate candle timestamp")
        seen.add(when)
        values = []
        for name in FIELDS:
            value = candle.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("Invalid OHLCV value")
            if name == "volume" and (value < 0 or int(value) != value):
                raise ValueError("Invalid volume")
            values.append(value)
        if values[1] < max(values[0], values[2], values[3]) or values[2] > min(values[0], values[1], values[3]):
            raise ValueError("Inconsistent OHLC values")
        selected.append((when, tuple(values)))
    return sorted(selected)


def save_json(path, value):
    publish_json(path, value)


def preserve(directory, receipt, body):
    stem = f"sample_{receipt['sample_number']:04d}"
    if body is not None:
        raw_path = directory / (stem + ".response.txt")
        with raw_path.open("xb") as out:
            out.write(body)
            out.flush()
            os.fsync(out.fileno())
        receipt["raw_response_file"] = raw_path.name
        receipt["raw_sha256"] = hashlib.sha256(body).hexdigest()
    save_json(directory / (stem + ".receipt.json"), receipt)


def summarize(directory, session_date, *, synthetic=False):
    states, rows, successful, errors = {}, [], 0, 0
    for path in sorted(directory.glob("sample_*.receipt.json")):
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if receipt.get("outcome") != "OK":
            errors += 1
            continue
        body = (directory / receipt["raw_response_file"]).read_bytes()
        if hashlib.sha256(body).hexdigest() != receipt["raw_sha256"]:
            raise ValueError("Saved response hash mismatch")
        candles = decode_candles(body, session_date, synthetic=synthetic)
        received = datetime.fromisoformat(receipt["received_at_utc"])
        successful += 1
        for when, values in candles:
            interval_end = when + timedelta(minutes=5)
            closed = received >= interval_end
            state = states.get(when)
            first = state is None
            if first:
                state = {"first_seen_at_utc": stamp(received), "values": values, "versions": 1,
                         "first_closed_seen_at_utc": None, "post_close_value_changes": 0,
                         "previous_observation_closed": False}
                states[when] = state
            changed = not first and state["values"] != values
            if changed:
                state["versions"] += 1
                if closed and state["previous_observation_closed"]:
                    state["post_close_value_changes"] += 1
            first_closed = closed and state["first_closed_seen_at_utc"] is None
            if first_closed:
                state["first_closed_seen_at_utc"] = stamp(received)
            state.update(values=values, last_seen_at_utc=stamp(received), previous_observation_closed=closed)
            rows.append({"sample_number": receipt["sample_number"], "received_at_utc": stamp(received),
                         "candle_start_et": when.astimezone(ET).isoformat(),
                         "interval_end_et": interval_end.astimezone(ET).isoformat(),
                         "interval_closed_at_receipt": closed, "first_closed_observation": first_closed,
                         "observed_version": state["versions"],
                         "value_change": "FIRST_SEEN" if first else "CHANGED" if changed else "UNCHANGED",
                         **dict(zip(FIELDS, values))})
    def write_csv(name, entries):
        if entries:
            with (directory / name).open("x", encoding="utf-8", newline="") as out:
                writer = csv.DictWriter(out, fieldnames=list(entries[0]))
                writer.writeheader()
                writer.writerows(entries)
    write_csv("candle_observations.csv", rows)
    intervals = []
    for when, state in sorted(states.items()):
        first_closed = state["first_closed_seen_at_utc"]
        intervals.append({"candle_start_et": when.astimezone(ET).isoformat(),
                          "first_seen_at_utc": state["first_seen_at_utc"],
                          "first_closed_seen_at_utc": first_closed or "",
                          "first_observed_closed_lag_seconds": "" if first_closed is None else
                              (datetime.fromisoformat(first_closed) - when - timedelta(minutes=5)).total_seconds(),
                          "last_seen_at_utc": state["last_seen_at_utc"],
                          "observed_versions": state["versions"],
                          "post_close_value_changes": state["post_close_value_changes"]})
    write_csv("interval_summary.csv", intervals)
    # Only boundaries crossed during this experiment count as availability evidence.
    observed_times = [datetime.fromisoformat(r["received_at_utc"]) for r in rows]
    low, high = (min(observed_times), max(observed_times)) if observed_times else (None, None)
    crossed = [when for when in states if low is not None and low < when + timedelta(minutes=5) <= high]
    return {"successful_responses": successful, "error_responses": errors,
            "candles_observed": len(states), "boundaries_crossed_with_observations": len(crossed),
            "unfinished_candle_observations": sum(not r["interval_closed_at_receipt"] for r in rows),
            "crossed_intervals_seen_after_close": sum(states[w]["first_closed_seen_at_utc"] is not None for w in crossed),
            "candles_changed_after_two_closed_observations": sum(s["post_close_value_changes"] > 0 for s in states.values()),
            "interpretation": "Interval closure is a clock classification, not provider finality. First seen is an upper bound at this polling cadence, not exact publication time."}


def ecfg_path():
    choices = [Path(os.environ["MB_SCHWAB_ECFG"])] if os.environ.get("MB_SCHWAB_ECFG") else []
    if os.environ.get("MB_VAULT"):
        choices.append(Path(os.environ["MB_VAULT"]) / "secure_schwabdev.ecfg")
    choices.extend([Path(r"C:\Users\danla\MBV\secure_schwabdev.ecfg"), Path.cwd() / "secure_schwabdev.ecfg"])
    for path in choices:
        if path.is_file():
            return path.resolve()
    raise CaptureInputError("Encrypted Schwab configuration was not found")


def capture(args):
    now = datetime.now(ET)
    session_date = date.fromisoformat(args.session_date) if args.session_date else now.date()
    synthetic = args.synthetic_scenario is not None
    start, end = synthetic_bounds(session_date) if synthetic else validate_window(session_date, now)
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", args.symbol):
        raise CaptureInputError("Invalid symbol")
    if not synthetic and (not 120 <= args.duration_seconds <= 1800 or not 15 <= args.interval_seconds <= 60):
        raise CaptureInputError("Use 120-1800 seconds duration and 15-60 seconds interval")
    if not synthetic and (end - now).total_seconds() < args.duration_seconds + 15:
        raise CaptureInputError("Not enough regular-session time remains for this capture")
    directory = Path(args.run_directory).resolve(strict=True)
    stop_file = directory / "stop-request.json"
    started = stamp(now)
    def phase(state):
        publish_json(directory / "producer_status.json", {"state": state,
                     "started_at_utc": started, "updated_at_utc": stamp(), "pid": os.getpid()}, replace=True)
    phase("WAITING_FOR_CREDENTIALS" if not synthetic else "STARTING")
    print(f"{'SYNTHETIC' if synthetic else 'LIVE API'} CANDLE EVIDENCE: {args.symbol}; {session_date}", flush=True)
    print("Separate output only. No production journal, membership change, or poller restart.", flush=True)
    print(f"Duration: {args.duration_seconds}s; request interval: {args.interval_seconds}s; HTTP timeout: 10s", flush=True)
    if synthetic:
        client = SyntheticClient(args.synthetic_scenario, session_date)
    else:
        config = ecfg_path()
        print(f"Encrypted config: {config}", flush=True)
        from mb_tools.schwab_secure import console_auth_callback, make_secure_schwab_client
        password = getpass.getpass("Encrypted config password: ")
        client = make_secure_schwab_client(config, password, timeout=10, call_on_auth=console_auth_callback)
        del password
    try:
        now = datetime.now(ET)
        if not synthetic:
            validate_window(session_date, now)
        if not synthetic and (end - now).total_seconds() < args.duration_seconds + 15:
            raise CaptureInputError("Authentication finished too late for this capture")
        started = stamp(now)
        phase("RUNNING")
        print(f"Evidence directory: {directory}", flush=True)
        began = time.monotonic()
        next_due, count, failures, status = began, 0, 0, "DURATION_COMPLETE"
        try:
            while next_due - began <= args.duration_seconds:
                delay = next_due - time.monotonic()
                while delay > 0 and not stop_file.exists():
                    time.sleep(min(delay, .1))
                    delay = next_due - time.monotonic()
                if stop_file.exists():
                    status = "STOPPED_BY_OPERATOR"
                    break
                if time.monotonic() - began > args.duration_seconds + 1:
                    break
                if not synthetic and datetime.now(ET) >= end:
                    status = "WINDOW_ENDED"
                    break
                count += 1
                sent = datetime.now(UTC)
                receipt = {"sample_number": count, "dispatched_at_utc": stamp(sent)}
                body, response, outcome = None, None, "REQUEST_ERROR"
                try:
                    response = client.price_history(args.symbol, frequencyType="minute", frequency=5,
                                                    startDate=start, endDate=end, needExtendedHoursData=False,
                                                    needPreviousClose=False)
                    body = response.content
                    received = datetime.now(UTC)
                    receipt.update(received_at_utc=stamp(received), http_status=response.status_code)
                    if response.ok:
                        candles = decode_candles(body, session_date, synthetic=synthetic)
                        outcome = "OK" if candles else "NO_CANDLES"
                        receipt["selected_candle_count"] = len(candles)
                        if candles:
                            receipt["last_candle_start_et"] = candles[-1][0].astimezone(ET).isoformat()
                            receipt["last_interval_closed_at_receipt"] = received >= candles[-1][0] + timedelta(minutes=5)
                    else:
                        outcome = "HTTP_ERROR"
                except Exception as error:
                    receipt["error_type"] = type(error).__name__
                    receipt.setdefault("received_at_utc", stamp(datetime.now(UTC)))
                    if response is not None:
                        outcome = "PAYLOAD_ERROR"
                receipt["outcome"] = outcome
                receipt["elapsed_seconds"] = (datetime.fromisoformat(receipt["received_at_utc"]) - sent).total_seconds()
                preserve(directory, receipt, body)
                failures = 0 if outcome == "OK" else failures + 1
                print(f"Sample {count:02d}: {outcome}; received {datetime.fromisoformat(receipt['received_at_utc']).astimezone(ET):%H:%M:%S} ET; last candle {receipt.get('last_candle_start_et', 'unavailable')}", flush=True)
                if failures >= 3 or receipt.get("http_status") in (401, 403):
                    status = "STOPPED_AFTER_ERRORS"
                    break
                # Skip missed request times; never burst to catch up.
                slot = max(round((next_due - began) / args.interval_seconds) + 1,
                           math.floor((time.monotonic() - began) / args.interval_seconds) + 1)
                next_due = began + slot * args.interval_seconds
        except KeyboardInterrupt:
            status = "STOPPED_BY_OPERATOR"
        result = summarize(directory, session_date, synthetic=synthetic)
        result.update(status=status, sample_count=count, session_date=str(session_date), symbol=args.symbol,
                      completed_at_utc=stamp(datetime.now(UTC)))
        save_json(directory / "manifest.json", result)
        phase(status)
        print(f"Capture: {status}; successful={result['successful_responses']}; errors={result['error_responses']}", flush=True)
        print(f"Boundaries observed: {result['boundaries_crossed_with_observations']}; unfinished observations: {result['unfinished_candle_observations']}", flush=True)
        print(f"Manifest: {directory / 'manifest.json'}", flush=True)
        print("Evidence preserved. Candle supervision remains active in this console.", flush=True)
        return 0 if status == "DURATION_COMPLETE" and result["successful_responses"] >= 2 and result["error_responses"] == 0 else 1
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--session-date")
    parser.add_argument("--duration-seconds", type=float, default=720)
    parser.add_argument("--interval-seconds", type=float, default=15)
    parser.add_argument("--run-directory", required=True)
    parser.add_argument("--synthetic-scenario", choices=("healthy", "api-error", "hang", "exit"))
    args = parser.parse_args()
    args.symbol = args.symbol.upper().strip()
    try:
        folder = Path(args.run_directory).resolve(strict=True)
        plan = json.loads((folder / "request_plan.json").read_text(encoding="utf-8"))
        if (plan["symbol"] != args.symbol or plan["session_date"] != args.session_date
                or plan["duration_seconds"] != args.duration_seconds
                or plan["interval_seconds"] != args.interval_seconds
                or plan.get("simulation", False) != (args.synthetic_scenario is not None)):
            raise CaptureInputError("Producer arguments differ from its immutable run plan")
        session = json.loads((folder.parent / "session_plan.json").read_text(encoding="utf-8"))
        lock_path = folder.parent.parent / f"{args.symbol}-{args.session_date}-producer.lock"
        with DirectoryLock(lock_path):
            return capture(args)
    except Exception as error:
        # Error text from authentication/network libraries may contain sensitive details.
        print(f"Candle capture stopped: {type(error).__name__}. No production process was changed.", file=sys.stderr)
        if isinstance(error, CaptureInputError):
            print(str(error), file=sys.stderr)
        try:
            publish_json(Path(args.run_directory) / "producer_status.json",
                         {"state": "FAILED", "started_at_utc": stamp(),
                          "updated_at_utc": stamp(), "error_type": type(error).__name__}, replace=True)
        except OSError:
            pass
        return 1


class SyntheticClient:
    """Explicit offline drill input. This class never imports API/auth code."""
    def __init__(self, scenario, session_date):
        self.scenario, self.session_date = scenario, session_date

    def price_history(self, symbol, **kwargs):
        from types import SimpleNamespace
        if self.scenario == "exit":
            os._exit(23)
        if self.scenario == "hang":
            time.sleep(120)
        if self.scenario == "api-error":
            return SimpleNamespace(content=b'{"simulation":true,"error":"synthetic"}', status_code=503, ok=False)
        start, end = synthetic_bounds(self.session_date)
        candle = min(datetime.now(ET).replace(second=0, microsecond=0), end-timedelta(minutes=5))
        candle = max(candle.replace(minute=candle.minute-candle.minute % 5), start)
        body = json.dumps({"symbol": symbol, "simulation": True, "candles": [{
            "datetime": int(candle.timestamp()*1000), "open": 100, "high": 102,
            "low": 99, "close": 101, "volume": 1000}]}).encode()
        return SimpleNamespace(content=body, status_code=200, ok=True)

    def close(self):
        pass


if __name__ == "__main__":
    raise SystemExit(main())
