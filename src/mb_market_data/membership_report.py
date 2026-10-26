"""Read effective Uni/Focus/Hot membership history from one saved daily journal.

Standard-library only, so this file can also be delivered as a standalone CLI.
It never opens the acquisition writer or scans quote observations.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
CHANNELS = ("uni", "focus", "hot")
CONTRACT = "nested-uni-focus-hot-v1"


class ReportError(ValueError):
    """A supplied journal cannot support a trustworthy membership report."""


def normalize_symbols(values: list[str]) -> list[str]:
    symbols = list(dict.fromkeys(word.upper() for value in values for word in value.split()))
    if not symbols:
        raise ReportError("Supply at least one space-delimited symbol.")
    for symbol in symbols:
        if len(symbol) > 64 or any(ord(char) < 33 or ord(char) > 126 for char in symbol):
            raise ReportError(f"Invalid symbol: {symbol!r}")
    return symbols


def report_cutoff(day: date, end_time: str | None, now: datetime) -> datetime:
    """Past/future dates default to 16:00 ET; today defaults to min(now, 16:00)."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ReportError("Current time must have a timezone.")
    local_now = now.astimezone(ET)
    if end_time is not None:
        try:
            clock = time.fromisoformat(end_time)
        except ValueError as exc:
            raise ReportError("End time must be HH:MM or HH:MM:SS in ET.") from exc
        if clock.tzinfo is not None:
            raise ReportError("End time is an ET wall-clock time; omit its UTC offset.")
        candidate = datetime.combine(day, clock, ET)
        # A repeated/missing DST wall time cannot be represented unambiguously
        # by the CLI's offset-free input. Regular market hours are unaffected.
        if candidate.utcoffset() != candidate.replace(fold=1).utcoffset():
            raise ReportError("End time is ambiguous or nonexistent on the ET DST transition.")
        return candidate
    close = datetime.combine(day, time(16), ET)
    return min(local_now, close) if day == local_now.date() else close


def default_database(root: Path, day: date) -> Path:
    return root / "output" / f"quote_observation_journal_v2_api_opening_{day}" / f"{day}.sqlite3"


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ReportError(f"Invalid journal timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReportError("Journal timestamp has no timezone.")
    return parsed.astimezone(ET)


def _history(connection: sqlite3.Connection, day: date, cutoff: datetime) -> tuple[int, dict]:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version not in (1, 2):
        raise ReportError(f"Unsupported journal schema: {version}")
    identity = connection.execute("SELECT * FROM store_identity WHERE singleton = 1").fetchone()
    if identity is None or identity["schema_version"] != version:
        raise ReportError("Missing or inconsistent journal identity.")
    if identity["session_date"] != day.isoformat():
        raise ReportError(f"Journal date is {identity['session_date']}; requested {day}.")
    if version == 2 and identity["membership_contract"] != CONTRACT:
        raise ReportError("Unsupported journal membership contract.")

    headers = connection.execute(
        "SELECT * FROM sampling_channel_revision WHERE session_date = ?", (day.isoformat(),)
    ).fetchall()
    histories = {channel: [] for channel in CHANNELS}
    grouped = {}
    for header in headers:
        effective = _timestamp(header["effective_at_utc"])
        if effective > cutoff:
            continue
        if effective.date() != day:
            raise ReportError("Membership effective date differs from the journal date.")
        channel = header["channel"]
        if channel not in CHANNELS:
            raise ReportError(f"Unsupported membership channel: {channel}")
        symbols = frozenset(row[0] for row in connection.execute(
            "SELECT symbol FROM sampling_channel_member "
            "WHERE session_date = ? AND channel = ? AND revision = ?",
            (day.isoformat(), channel, header["revision"]),
        ))
        if len(symbols) != header["symbol_count"]:
            raise ReportError(f"Membership count mismatch: {channel} r{header['revision']}")
        item = {
            "effective": effective, "revision": header["revision"], "symbols": symbols,
            "source": header["source"], "reason": header["reason"], "published_at_et": None,
        }
        histories[channel].append(item)
        grouped.setdefault(header["revision"], {})[channel] = item

    if version == 2:
        parents = connection.execute(
            "SELECT * FROM sampling_membership_revision WHERE session_date = ?", (day.isoformat(),)
        ).fetchall()
        selected = {row["revision"]: row for row in parents if _timestamp(row["effective_at_utc"]) <= cutoff}
        if set(selected) != set(grouped):
            raise ReportError("Hierarchy and channel revision headers disagree.")
        for revision, parent in selected.items():
            channels = grouped[revision]
            if set(channels) != set(CHANNELS):
                raise ReportError(f"Incomplete Uni/Focus/Hot hierarchy: r{revision}")
            if parent["membership_contract"] != CONTRACT:
                raise ReportError(f"Unsupported membership contract: r{revision}")
            effective = _timestamp(parent["effective_at_utc"])
            if any(item["effective"] != effective or item["source"] != parent["source"]
                   or item["reason"] != parent["reason"] for item in channels.values()):
                raise ReportError(f"Hierarchy/channel provenance mismatch: r{revision}")
            if not channels["hot"]["symbols"] <= channels["focus"]["symbols"] <= channels["uni"]["symbols"]:
                raise ReportError(f"Membership nesting violation: r{revision}")
            published = _timestamp(parent["published_at_utc"])
            if published > effective:
                raise ReportError(f"Publication follows effective time: r{revision}")
            for item in channels.values():
                item["published_at_et"] = published.isoformat()
    for history in histories.values():
        history.sort(key=lambda item: (item["effective"], item["revision"]))
        if any(right["revision"] <= left["revision"] for left, right in zip(history, history[1:])):
            raise ReportError("Membership revision numbers are not in effective-time order.")
    return version, histories


def _channel_report(history: list[dict], symbol: str, start: datetime, cutoff: datetime) -> dict | None:
    if not history:
        return None
    intervals = []
    state = None
    cursor = start
    revisions = []
    for item in history:
        effective = item["effective"]
        member = symbol in item["symbols"]
        revisions.append({
            "revision": item["revision"], "effective_at_et": effective.isoformat(),
            "published_at_et": item["published_at_et"], "member": member,
            "source": item["source"], "reason": item["reason"],
        })
        if member != state:
            if cursor < effective:
                intervals.append({"from_et": cursor.isoformat(), "to_et": effective.isoformat(), "member": state})
            cursor = effective
            state = member
    intervals.append({"from_et": cursor.isoformat(), "to_et": cutoff.isoformat(), "member": state})
    return {
        "at_cutoff": state, "ever_member": any(item["member"] for item in intervals),
        "known_from_et": history[0]["effective"].isoformat(),
        "intervals": intervals, "revisions": revisions,
    }


def build_report(database: Path, day: date, cutoff: datetime, symbols: list[str], *, missing_ok: bool = False) -> dict:
    """Use one read-only SQLite transaction; absent auto-selected days yield NULL."""
    if cutoff.tzinfo is None or cutoff.utcoffset() is None or cutoff.astimezone(ET).date() != day:
        raise ReportError("Cutoff must be timezone-aware and on the requested ET date.")
    cutoff = cutoff.astimezone(ET)
    symbols = normalize_symbols(symbols)
    report = {
        "contract": "daily-symbol-membership-report-v1", "date": day.isoformat(),
        "cutoff_et": cutoff.isoformat(), "database": str(database.resolve()),
        "basis": "effective membership in currently saved revisions; not an as-known-then replay",
        "status": "NO_DATA", "reason": None, "schema_version": None,
        "symbols": {symbol: None for symbol in symbols},
    }
    if not database.is_file():
        if not missing_ok:
            raise ReportError(f"Explicit database does not exist: {database}")
        report["reason"] = "no_daily_journal"
        return report
    try:
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=3) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            version, histories = _history(connection, day, cutoff)
    except sqlite3.Error as exc:
        raise ReportError(f"Cannot read journal: {exc}") from exc
    finally:
        if "connection" in locals():
            connection.close()
    report["schema_version"] = version
    if not any(histories.values()):
        report["reason"] = "no_membership_revisions_up_to_cutoff"
        return report
    report["status"] = "OK"
    start = datetime.combine(day, time.min, ET)
    for symbol in symbols:
        report["symbols"][symbol] = {
            channel: _channel_report(histories[channel], symbol, start, cutoff) for channel in CHANNELS
        }
    return report


def _clock(value: str) -> str:
    return datetime.fromisoformat(value).time().isoformat()


def format_text(report: dict) -> str:
    lines = [f"Membership report: {report['date']} through {_clock(report['cutoff_et'])} ET",
             f"Journal: {report['database']}",
             "Effective membership from currently saved history. All times ET."]
    if report["status"] == "NO_DATA":
        lines.append(f"No membership data: {report['reason']}")
        lines.extend(f"{symbol}: NULL" for symbol in report["symbols"])
        return "\n".join(lines)
    for symbol, channels in report["symbols"].items():
        lines.append(f"\n{symbol}")
        for name, channel in channels.items():
            if channel is None:
                lines.append(f"  {name.upper():5} NULL (no channel history)")
                continue
            state = "YES" if channel["at_cutoff"] else "NO"
            spans = []
            for interval in channel["intervals"]:
                if interval["member"]:
                    begin, end = _clock(interval["from_et"]), _clock(interval["to_et"])
                    spans.append(f"{begin} to {end}" if begin != end else f"at {begin}")
            lines.append(f"  {name.upper():5} at cutoff: {state:3} | member: {', '.join(spans) or 'never in recorded history'}")
        known = sorted({channel["known_from_et"] for channel in channels.values() if channel})
        if len(known) == 1:
            lines.append(f"  History starts {_clock(known[0])}; earlier membership is unknown.")
        else:
            for name, channel in channels.items():
                if channel:
                    lines.append(f"  {name.upper()} history starts {_clock(channel['known_from_et'])}; earlier membership is unknown.")
    lines.append("\nMembership intervals end when the next state starts; 'at cutoff' includes that instant.")
    lines.append("Membership does not prove quote collection or poller health.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report saved Uni/Focus/Hot membership intervals. Read-only; no API or password.")
    parser.add_argument("symbols", nargs="+", help='Space-delimited symbols, quoted or unquoted')
    parser.add_argument("--date", type=date.fromisoformat, help="Requested ET date YYYY-MM-DD (default: today ET)")
    parser.add_argument("--end-time", help="ET HH:MM[:SS]; default today min(now,16:00), other dates 16:00")
    parser.add_argument("--market-data-root", type=Path, default=Path.cwd(), help="Repository root (default: current directory)")
    parser.add_argument("--database", type=Path, help="Explicit single journal, including legacy or isolated experiments")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    day = args.date or now.astimezone(ET).date()
    try:
        cutoff = report_cutoff(day, args.end_time, now)
        database = args.database or default_database(args.market_data_root, day)
        report = build_report(database, day, cutoff, args.symbols, missing_ok=args.database is None)
    except (OSError, ReportError) as exc:
        print(f"Membership report ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2) if args.format == "json" else format_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
