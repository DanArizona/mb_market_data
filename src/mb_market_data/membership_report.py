"""Read effective Uni/Focus/Hot membership history from one saved daily journal.

Standard-library only, so this file can also be delivered as a standalone CLI.
It never opens the acquisition writer or scans quote observations.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
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


def _metadata(value: str) -> dict:
    try:
        result = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ReportError("Invalid membership metadata JSON.") from exc
    if not isinstance(result, dict):
        raise ReportError("Membership metadata must be an object.")
    return result


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
            "metadata": _metadata(header["metadata_json"]),
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
            metadata = _metadata(parent["metadata_json"])
            if any(item["effective"] != effective or item["source"] != parent["source"]
                   or item["reason"] != parent["reason"] or item["metadata"] != metadata
                   for item in channels.values()):
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
    changes = []
    for item in history:
        effective = item["effective"]
        member = symbol in item["symbols"]
        revisions.append({
            "revision": item["revision"], "effective_at_et": effective.isoformat(),
            "published_at_et": item["published_at_et"], "member": member,
            "source": item["source"], "reason": item["reason"],
            "metadata": item["metadata"],
        })
        if member != state:
            changes.append({
                **revisions[-1],
                "action": "joined" if member else ("not a member" if state is None else "left"),
            })
            if cursor < effective:
                intervals.append({"from_et": cursor.isoformat(), "to_et": effective.isoformat(), "member": state})
            cursor = effective
            state = member
    intervals.append({"from_et": cursor.isoformat(), "to_et": cutoff.isoformat(), "member": state})
    return {
        "at_cutoff": state, "ever_member": any(item["member"] for item in revisions),
        "known_from_et": history[0]["effective"].isoformat(),
        "intervals": intervals, "revisions": revisions, "changes": changes,
    }


def _decision_metadata(metadata: dict, channel: str) -> dict | None:
    """Recognize selector metadata, including preserved carry-forward metadata.

    A Focus ledger hash on r1 must never be used to explain Uni membership.
    Paths and filenames alone are not a binding to the published revision.
    """
    pending = [metadata]
    matches = []
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            digest = value.get("decision_ledger_sha256")
            role = ("uni" if any(key in value for key in (
                "universe_manifest_sha256", "universe_source_session_date", "selector_version"))
                else "focus" if any(key in value for key in (
                    "focus_symbols_sha256", "ranked_focus_symbols")) else None)
            if role == channel and isinstance(digest, str) and len(digest) == 64 \
                    and all(char in "0123456789abcdefABCDEF" for char in digest):
                matches.append(value)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    if len({item["decision_ledger_sha256"].lower() for item in matches}) == 1:
        return matches[0]
    return None


class DecisionEvidence:
    """Read only ledger files whose bytes match a published selector hash."""

    def __init__(self, root: Path | None, extra_roots: list[Path] | None = None):
        extras = list(extra_roots or [])
        self.roots = {
            "uni": ([root / "output" / "daily_universe_production"] if root else []) + extras,
            "focus": ([root.parent / "schwab_watchlists" / "output" / "ov_focus_production",
                       root / "output" / "ov_focus_production"] if root else []) + extras,
        }
        self.paths = {}
        self.digests = {}
        self.cache = {}

    def ledger(self, channel: str, metadata: dict, members: frozenset) -> tuple[dict | None, str | None, str | None]:
        digest = metadata["decision_ledger_sha256"].lower()
        key = (channel, digest)
        if key not in self.cache:
            filename = "decision_ledger.csv" if channel == "uni" else "focus_decision_ledger.csv"
            if channel not in self.paths:
                paths = set()
                for root in self.roots[channel]:
                    try:
                        if root.is_dir():
                            paths.update(path.resolve() for path in root.rglob(filename) if path.is_file())
                    except OSError:
                        # Missing reason evidence must not erase valid membership.
                        continue
                self.paths[channel] = sorted(paths)
            found = None
            for path in self.paths[channel]:
                try:
                    if path not in self.digests:
                        self.digests[path] = hashlib.sha256(path.read_bytes()).hexdigest()
                    if self.digests[path] != digest:
                        continue
                    payload = path.read_bytes()
                    if hashlib.sha256(payload).hexdigest() != digest:
                        continue
                    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""))
                    if not {"symbol", "decision", "primary_reason"} <= set(reader.fieldnames or []):
                        raise ValueError("decision ledger columns are incomplete")
                    rows = {}
                    for row in reader:
                        symbol = (row.get("symbol") or "").strip().upper()
                        if not symbol or symbol in rows:
                            raise ValueError("decision ledger has blank or duplicate symbols")
                        if row.get("decision") not in ("include", "reject", "exclude"):
                            raise ValueError("decision ledger has an unsupported decision")
                        rows[symbol] = row
                    found = (rows, str(path), None)
                    break
                except (OSError, UnicodeError, csv.Error, ValueError) as exc:
                    if self.digests.get(path) == digest:
                        found = (None, str(path), f"matching decision ledger is unreadable or invalid: {exc}")
                        break
            self.cache[key] = found or (None, None, "no saved decision ledger matches the published hash")
        rows, path, problem = self.cache[key]
        if rows is not None and {symbol for symbol, row in rows.items() if row["decision"] == "include"} != members:
            return None, path, "decision ledger inclusion set differs from the published channel membership"
        return rows, path, problem


def _selector_explanation(symbol: str, channel: str, item: dict, evidence: DecisionEvidence) -> dict | None:
    metadata = _decision_metadata(item["metadata"], channel)
    if metadata is None:
        return None
    rows, path, problem = evidence.ledger(channel, metadata, item["symbols"])
    source_date = metadata.get("universe_source_session_date")
    base = {"kind": "selector", "ledger": path, "ledger_sha256": metadata["decision_ledger_sha256"],
            "source_session_date": source_date}
    context = f" (source session={source_date})" if source_date else ""
    if problem:
        return {**base, "kind": "unavailable", "message": f"reason unavailable: {problem}{context}"}
    row = rows.get(symbol)
    if row is None:
        return {**base, "kind": "unavailable", "message": "reason unavailable: no decision for this symbol in the verified ledger" + context}
    codes = [value.strip() for value in (row.get("reason_codes") or row["primary_reason"]).split(";") if value.strip()]
    details = []
    for key, label in (("calculated_market_cap", "market cap"), ("close_price", "close"),
                       ("total_volume", "volume"), ("ov_decision", "OV"),
                       ("eligible_rank", "eligible rank")):
        if row.get(key):
            details.append(f"{label}={row[key]}")
    if channel == "focus" and metadata.get("requested_limit") is not None:
        details.append(f"selection limit={metadata['requested_limit']}")
    if source_date:
        details.append(f"source session={source_date}")
    message = "; ".join(code.replace("_", " ") for code in codes) or "reason not recorded"
    if details:
        message += " (" + "; ".join(details) + ")"
    return {**base, "message": message, "codes": codes, "decision": row["decision"], "values": row}


def _explain_channel(channel: dict, history: list[dict], symbol: str, name: str, evidence: DecisionEvidence) -> None:
    by_revision = {item["revision"]: item for item in history}
    for change in channel["changes"]:
        item = by_revision[change["revision"]]
        explanation = _selector_explanation(symbol, name, item, evidence)
        change["explanation"] = explanation or {
            "kind": "journal" if item["reason"] else "unavailable",
            "message": ("revision reason: " + item["reason"]) if item["reason"] else "reason not recorded",
        }
    if not channel["ever_member"]:
        # Later Focus metadata does not supersede the Uni selector's evidence.
        selected = next((item for item in reversed(history)
                         if _decision_metadata(item["metadata"], name) is not None), None)
        channel["never_reason"] = (_selector_explanation(symbol, name, selected, evidence) if selected else None) or {
            "kind": "unavailable", "message": "reason unavailable: no symbol-specific exclusion decision recorded",
        }


def build_report(database: Path, day: date, cutoff: datetime, symbols: list[str], *, missing_ok: bool = False,
                 market_data_root: Path | None = None, evidence_roots: list[Path] | None = None) -> dict:
    """Use one read-only SQLite transaction; absent auto-selected days yield NULL."""
    if cutoff.tzinfo is None or cutoff.utcoffset() is None or cutoff.astimezone(ET).date() != day:
        raise ReportError("Cutoff must be timezone-aware and on the requested ET date.")
    cutoff = cutoff.astimezone(ET)
    symbols = normalize_symbols(symbols)
    report = {
        "contract": "daily-symbol-membership-report-v2", "date": day.isoformat(),
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
    evidence = DecisionEvidence(market_data_root, evidence_roots)
    for symbol in symbols:
        report["symbols"][symbol] = {
            channel: _channel_report(histories[channel], symbol, start, cutoff) for channel in CHANNELS
        }
        for name, channel in report["symbols"][symbol].items():
            if channel is not None:
                _explain_channel(channel, histories[name], symbol, name, evidence)
    return report


def _clock(value: str) -> str:
    return datetime.fromisoformat(value).time().isoformat()


def format_text(report: dict) -> str:
    lines = [f"Membership report: {report['date']} through {_clock(report['cutoff_et'])} ET",
             f"Journal: {report['database']}",
             "Effective membership changes from currently saved history. All times ET."]
    if report["status"] == "NO_DATA":
        lines.append(f"No membership data: {report['reason']}")
        lines.extend(f"{symbol}: NULL" for symbol in report["symbols"])
        return "\n".join(lines)
    for symbol, channels in report["symbols"].items():
        lines.append(f"\n{symbol}")
        for name, channel in channels.items():
            if channel is None:
                lines.append(f"  {name.upper()}: NULL (no channel history)")
                continue
            if report["schema_version"] == 2 and name != "uni":
                parent = channels["uni" if name == "focus" else "focus"]
                if parent is not None and not parent["ever_member"]:
                    continue
                if name == "hot" and channels["uni"] is not None and not channels["uni"]["ever_member"]:
                    continue
            if not channel["ever_member"]:
                message = channel["never_reason"]["message"]
                lines.append(f"  {name.upper()}: never a member | {' '.join(message.split())}")
                continue
            state = "YES" if channel["at_cutoff"] else "NO"
            lines.append(f"  {name.upper()}: at cutoff: {state}")
            for change in channel["changes"]:
                if change["action"] == "not a member":
                    continue
                message = " ".join(change["explanation"]["message"].split())
                source = " ".join(change["source"].split())
                lines.append(f"    {_clock(change['effective_at_et'])} {change['action']} | {message} [r{change['revision']}; {source}]")
        known = sorted({channel["known_from_et"] for channel in channels.values() if channel})
        if len(known) == 1:
            lines.append(f"  History starts {_clock(known[0])}; earlier membership is unknown.")
        else:
            for name, channel in channels.items():
                if channel:
                    lines.append(f"  {name.upper()} history starts {_clock(channel['known_from_et'])}; earlier membership is unknown.")
    lines.append("\n'Never a member' covers recorded history through the cutoff; earlier membership is unknown.")
    lines.append("Unchanged membership is not repeated.")
    if report["schema_version"] == 2:
        lines.append("Nested child channels are omitted when their parent was never a member.")
    lines.append("Membership does not prove quote collection or poller health.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report saved Uni/Focus/Hot membership changes and recorded reasons. Read-only; no API or password.")
    parser.add_argument("symbols", nargs="+", help='Space-delimited symbols, quoted or unquoted')
    parser.add_argument("--date", type=date.fromisoformat, help="Requested ET date YYYY-MM-DD (default: today ET)")
    parser.add_argument("--end-time", help="ET HH:MM[:SS]; default today min(now,16:00), other dates 16:00")
    parser.add_argument("--market-data-root", type=Path, default=Path.cwd(), help="Repository root (default: current directory)")
    parser.add_argument("--database", type=Path, help="Explicit single journal, including legacy or isolated experiments")
    parser.add_argument("--evidence-root", type=Path, action="append", default=[],
                        help="Additional folder to search for hash-matching Uni/Focus decision ledgers (repeatable)")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    day = args.date or now.astimezone(ET).date()
    try:
        cutoff = report_cutoff(day, args.end_time, now)
        database = args.database or default_database(args.market_data_root, day)
        report = build_report(database, day, cutoff, args.symbols, missing_ok=args.database is None,
                              market_data_root=args.market_data_root, evidence_roots=args.evidence_root)
    except (OSError, ReportError) as exc:
        print(f"Membership report ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2) if args.format == "json" else format_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
