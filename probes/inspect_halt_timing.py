"""Read one saved Nasdaq halt and nearby quote timestamps; no provider client."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
UTC = timezone.utc


def aware(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError(f"Timestamp has no UTC offset: {value}")
    return result


def clock(value: str | datetime | None) -> str:
    if value is None:
        return "-"
    stamp = aware(value) if isinstance(value, str) else value
    return stamp.astimezone(ET).isoformat(timespec="milliseconds")[11:23]


def provider_clock(value: int | None, session: date) -> str:
    if value is None:
        return "-"
    stamp = datetime.fromtimestamp(value / 1000, UTC).astimezone(ET)
    # An old trade must not look like a same-day observation at the same hour.
    return clock(stamp) if stamp.date() == session else stamp.isoformat(timespec="milliseconds")


def reported_start(record: dict) -> datetime:
    day = datetime.strptime(record["halt_date"], "%m/%d/%Y").date()
    value = "".join(record["halt_time"].split())
    result = datetime.combine(day, time.fromisoformat(value))
    if result.tzinfo is not None:
        raise ValueError("Nasdaq halt_time unexpectedly contains a UTC offset")
    return result.replace(tzinfo=ET)


def episodes_for(path: Path, symbol: str, session: date) -> list[tuple]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Nasdaq events file must be an object")
    result = []
    for key, episode in payload.items():
        if not key.startswith(symbol + "@"):
            continue
        record = episode["record"]
        if record["symbol"].upper() != symbol:
            raise ValueError("Nasdaq episode key and symbol disagree")
        start = reported_start(record)
        if start.date() != session:
            continue
        if abs((aware(key.split("@", 1)[1]) - start).total_seconds()) > 0.001:
            raise ValueError("Nasdaq episode key and reported halt time disagree")
        result.append((start, key, episode))
    return sorted(result, key=lambda item: item[0])


def print_first_capture(root: Path, symbol: str, start: datetime, episode: dict) -> None:
    # capture_file in events.json points to the LATEST capture. Locate the first
    # receipt independently, using the immutable producer filename convention.
    first = aware(episode["first_observed_at"]).astimezone(UTC)
    candidates = sorted((root / "captures").glob(first.strftime("%H%M%S%f") + "-*.json"))
    for path in candidates:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if aware(payload["observed_at"]) != first:
            continue
        matches = [
            record for record in payload["records"]
            if record.get("symbol", "").upper() == symbol
            and reported_start(record) == start
        ]
        if not matches:
            continue
        raw_path = path.with_suffix(".xml")
        raw_match = (
            hashlib.sha256(raw_path.read_bytes()).hexdigest() == payload.get("raw_sha256")
            if raw_path.is_file() else "XML missing"
        )
        print(
            f"FIRST_CAPTURE: {path.name} | mode={payload.get('mode')} | "
            f"halt_time={matches[0]['halt_time']} | raw_sha256_matches={raw_match}"
        )
        return
    print("FIRST_CAPTURE: not found; first-observed time below is from events.json")


def quote_rows(database: Path, session: date, symbol: str, channel: str,
               start: datetime, before: int, after: int) -> list[sqlite3.Row]:
    lower = (start - timedelta(seconds=before)).astimezone(UTC).isoformat()
    upper = (start + timedelta(seconds=after)).astimezone(UTC).isoformat()
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.row_factory = sqlite3.Row
        # Compare parsed timestamps: persisted UTC strings use Z, while some
        # earlier artifacts use +00:00 or a different number of fractional digits.
        return list(connection.execute(
            """SELECT a.acquisition_id, a.scheduled_at_utc, a.dispatched_at_utc,
                      a.completed_at_utc, o.request_started_at_utc,
                      o.response_received_at_utc, o.status, o.realtime,
                      o.quote_time_ms, o.quote_trade_time_ms,
                      o.quote_security_status, o.quote_last_price, o.quote_total_volume
               FROM quote_acquisition AS a
               JOIN quote_observation AS o ON o.acquisition_id=a.acquisition_id
               WHERE a.session_date=? AND a.channel=? AND o.symbol=?
                 AND julianday(a.completed_at_utc) BETWEEN julianday(?) AND julianday(?)
               ORDER BY julianday(a.completed_at_utc), a.acquisition_id""",
            (session.isoformat(), channel, symbol, lower, upper),
        ))
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-data-root", type=Path, required=True)
    parser.add_argument("--session-date", type=date.fromisoformat, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--halt-time", type=time.fromisoformat, required=True,
                        help="Nasdaq start in Eastern time; whole seconds match any fraction")
    parser.add_argument("--channel", choices=("hot", "focus", "uni"), default="hot")
    parser.add_argument("--before-seconds", type=int, default=35)
    parser.add_argument("--after-seconds", type=int, default=15)
    args = parser.parse_args()
    if not 0 <= args.before_seconds <= 600 or not 0 <= args.after_seconds <= 600:
        parser.error("Window sizes must be between 0 and 600 seconds")
    if args.halt_time.tzinfo is not None:
        parser.error("--halt-time is an Eastern wall-clock time without an offset")
    symbol = args.symbol.strip().upper()
    output = args.market_data_root / "output"
    daily = output / "daily_session" / args.session_date.isoformat() / "nasdaq"
    database = (
        output / f"quote_observation_journal_v2_api_opening_{args.session_date}"
        / f"{args.session_date}.sqlite3"
    )
    try:
        episodes = episodes_for(daily / "events.json", symbol, args.session_date)
        selected = [
            item for item in episodes
            if (item[0].time() if args.halt_time.microsecond else
                item[0].time().replace(microsecond=0)) == args.halt_time
        ]
        if len(selected) != 1:
            raise ValueError(f"Expected one matching Nasdaq episode, found {len(selected)}")
        start, key, episode = selected[0]
        rows = quote_rows(database, args.session_date, symbol, args.channel,
                          start, args.before_seconds, args.after_seconds)
        print(f"HALT_TIMING_BEGIN | {args.session_date} | {symbol} | {args.channel}")
        print(f"NASDAQ_KEY: {key}")
        print(f"NASDAQ_START_ET: {start.isoformat()}")
        print(f"NASDAQ_FIRST_OBSERVED_ET: {aware(episode['first_observed_at']).astimezone(ET).isoformat()}")
        print(f"NASDAQ_REASON: {episode['record'].get('reason_code')}")
        previous = [item for item in episodes if item[0] < start]
        if previous:
            prior_start, _, prior = previous[-1]
            record = prior["record"]
            print(f"PREVIOUS_NASDAQ: start={prior_start.isoformat()} | "
                  f"reported_trade_resumption={record.get('resumption_date')} "
                  f"{record.get('resumption_trade_time')}")
        print_first_capture(daily, symbol, start, episode)
        print("All table times ET; local times displayed to milliseconds. "
              "Provider dates are included when outside this session.")
        print("response_minus_nasdaq_start_s uses full saved precision; "
              "negative means received before the Nasdaq start.")
        print("scheduled | dispatched | requested | response | available | provider_quote | "
              "provider_trade | security_status | price | volume | realtime | api_result | response_minus_nasdaq_start_s")
        for row in rows:
            received = row["response_received_at_utc"]
            delta = f"{(aware(received) - start).total_seconds():+.6f}" if received else "-"
            values = [
                clock(row["scheduled_at_utc"]), clock(row["dispatched_at_utc"]),
                clock(row["request_started_at_utc"]), clock(received),
                clock(row["completed_at_utc"]), provider_clock(row["quote_time_ms"], args.session_date),
                provider_clock(row["quote_trade_time_ms"], args.session_date), row["quote_security_status"],
                row["quote_last_price"], row["quote_total_volume"], row["realtime"],
                row["status"], delta,
            ]
            print(" | ".join("-" if value is None else str(value) for value in values))
        print(f"ROWS: {len(rows)} | window={args.before_seconds}s before / {args.after_seconds}s after")
        print("Provider quote/trade times are not security-status transition timestamps.")
        print("HALT_TIMING_END")
        return 0 if rows else 1
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(f"HALT_TIMING_ERROR: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
