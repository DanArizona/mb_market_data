"""Isolated, explicit-cadence acquisition for schema-v2 sampling channels.

Review-stage market-data code, independent of the Coordinator package. It uses
the existing batch collector and journal. No production cadence or authentication
is installed here. The caller supplies a client with bounded request timeouts.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

from mb_market_data.quote_observation_store import OBSERVATION_COLUMNS, PollRunProvenance
from mb_market_data.sampling_membership import SamplingChannel
from mb_market_data.schwab_quotes import QuoteBatchResult, QuoteResult, QuoteStatus, fetch_quotes_batched
from mb_market_data.watchlist_polling import ET, PollRequest, PollSlot, PollWindow, SkippedPollSlot

MARKER = "isolated_hot_acquisition_v1"
_JSON_SCALAR_TYPES = frozenset((str,int,float,bool,type(None)))


def plain(value):
    if type(value) in _JSON_SCALAR_TYPES:return value
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    return value


def canonical(value):
    return json.dumps(plain(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Acquisition times must be timezone-aware")
    return value


def text(value):
    return aware(value).astimezone(timezone.utc).isoformat()


def stamp(value):
    return aware(datetime.fromisoformat(value))


@dataclass(frozen=True, init=False)
class ExplicitPollSlot(PollSlot):
    """A PollSlot with a declared grid, including Hot without a global default."""
    seconds: tuple[int, ...]

    def __init__(self, watchlist_kind, scheduled_at, seconds):
        # Preserve the accepted explicit-grid positional API when the base
        # PollSlot also carries the new optional cadence_profile field.
        object.__setattr__(self, 'watchlist_kind', watchlist_kind)
        object.__setattr__(self, 'scheduled_at', scheduled_at)
        object.__setattr__(self, 'cadence_profile', 'explicit-grid')
        object.__setattr__(self, 'seconds', seconds)
        self.__post_init__()

    def __post_init__(self):
        aware(self.scheduled_at)
        if not isinstance(self.watchlist_kind, SamplingChannel):
            raise ValueError("Explicit slot requires a sampling channel")
        if self.scheduled_at.astimezone(ET).second not in self.seconds or self.scheduled_at.microsecond:
            raise ValueError("Slot is outside the declared whole-second grid")


@dataclass(frozen=True, slots=True)
class AcquisitionSettings:
    channel: SamplingChannel
    seconds: tuple[int, ...]
    window: PollWindow
    batch_size: int
    fields: str
    source_label: str
    max_dispatch_lateness_seconds: float
    retry_seconds: float

    def __post_init__(self):
        if not isinstance(self.channel, SamplingChannel):
            raise ValueError("channel must be a SamplingChannel")
        if not isinstance(self.seconds, tuple) or not self.seconds or any(type(s) is not int or not 0 <= s <= 59 for s in self.seconds) or tuple(sorted(set(self.seconds))) != self.seconds:
            raise ValueError("seconds must be an explicit sorted tuple of unique integers 0..59")
        if type(self.batch_size) is not int or not 1 <= self.batch_size <= 500:
            raise ValueError("batch_size must be in 1..500")
        for v in (self.fields, self.source_label):
            if not isinstance(v, str) or not v.strip():
                raise ValueError("fields and source_label must be explicit nonblank strings")
        for name in ("max_dispatch_lateness_seconds", "retry_seconds"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or v < 0 or (name == "retry_seconds" and v == 0):
                raise ValueError(f"{name} must be finite and {'positive' if name == 'retry_seconds' else 'nonnegative'}")
        if (self.window.end_at - timedelta(microseconds=1)).astimezone(ET).date() != self.window.session_date:
            raise ValueError("Acquisition window must remain within one ET session date")

    def payload(self):
        return {"watchlist_kind": self.channel.value, "poll_seconds": list(self.seconds),
                "poll_window_start_et": self.window.start_at.astimezone(ET).isoformat(),
                "poll_window_end_et": self.window.end_at.astimezone(ET).isoformat(),
                "batch_size": self.batch_size, "fields": self.fields, "source_label": self.source_label,
                "max_dispatch_lateness_seconds": self.max_dispatch_lateness_seconds, "retry_seconds": self.retry_seconds}


def next_slot(settings, *, after):
    aware(after)
    start, end = settings.window.start_at.astimezone(ET), settings.window.end_at.astimezone(ET)
    cursor = max(after.astimezone(ET), start - timedelta(microseconds=1))
    minute = cursor.replace(second=0, microsecond=0)
    while minute < end:
        for second in settings.seconds:
            value = minute.replace(second=second)
            if value >= end:
                return None
            if value > cursor and value >= start:
                return ExplicitPollSlot(settings.channel, value, settings.seconds)
        minute += timedelta(minutes=1)
    return None


class AcquisitionBusy(RuntimeError):
    pass


@contextmanager
def owner_lock(path):
    with path.open("a+b") as f:
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:
            f.write(b"0"); f.flush()
        f.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AcquisitionBusy("This channel acquisition ledger is owned by another cycle") from exc
        try:
            yield
        finally:
            f.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def encode_result(result):
    return {"request_count": result.request_count, "batch_size": result.batch_size,
            "unexpected_symbols": list(result.unexpected_symbols), "results": [
                {"symbol": r.symbol, "status": r.status.value, "quote": plain(r.quote), "detail": r.detail,
                 "batch_number": r.batch_number, "request_started_at_utc": text(r.request_started_at_utc),
                 "response_received_at_utc": text(r.response_received_at_utc) if r.response_received_at_utc else None}
                for r in result.results]}


def decode_result(value):
    return QuoteBatchResult(tuple(QuoteResult(r["symbol"], QuoteStatus(r["status"]), r["quote"], r["detail"], r["batch_number"],
        stamp(r["request_started_at_utc"]), stamp(r["response_received_at_utc"]) if r["response_received_at_utc"] else None)
        for r in value["results"]), value["request_count"], value["batch_size"], tuple(value["unexpected_symbols"]))


class IsolatedSamplingPoller:
    def __init__(self, journal, *, settings: AcquisitionSettings, client, clock=None):
        if not callable(getattr(client, "quotes", None)):
            raise ValueError("The supplied quote client must have a quotes() method")
        if journal.schema_version != 2 or journal.session_date != settings.window.session_date:
            raise ValueError("An matching-session schema-v2 journal is required")
        if any(p.startswith("quote_observation_journal_v2_api_opening_") for p in journal.database_path.resolve().parts):
            raise ValueError("Production opening journals are excluded")
        revisions = journal.membership_revisions_in_effective_order()
        if not revisions or revisions[0].metadata.get(MARKER) is not True:
            raise ValueError("Journal requires the explicit isolated Hot acquisition marker")
        self.journal, self.settings, self.client = journal, settings, client
        # Derived values only: live evidence hashes and journal rows are still checked.
        # A bounded per-instance cache is discarded on restart and is never persisted.
        self._normalization_cache = OrderedDict()
        self.normalization_cache_hits = self.normalization_cache_misses = 0
        self.verification_seconds = self.normalization_seconds = 0.0
        self.verification_reads = 0
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        canonical_journal = journal.database_path.resolve()
        self.path = canonical_journal.with_name(canonical_journal.stem + "." + settings.channel.value + ".poller.sqlite3")
        self.lock_path = self.path.with_suffix(".lock")
        self.identity = {"contract": MARKER, "journal": str(journal.database_path.resolve()),
                         "baseline_sha256": revisions[0].content_sha256, "settings": settings.payload()}
        self.run = PollRunProvenance("isolated-" + settings.channel.value + "-" + uuid.uuid4().hex,
            aware(self.clock()), "experimental-hot-acquisition-v1", {**settings.payload(), "experimental": True, MARKER: True})

    def initialize(self):
        with owner_lock(self.lock_path), closing(sqlite3.connect(self.path, timeout=5)) as c, c:
            c.execute("BEGIN IMMEDIATE")
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "acquisition_identity" not in tables:
                raise ValueError("Existing sidecar is not an acquisition ledger")
            c.execute("CREATE TABLE IF NOT EXISTS acquisition_identity(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL)")
            row = c.execute("SELECT payload FROM acquisition_identity WHERE id=1").fetchone()
            if row is not None and row[0] != canonical(self.identity):
                raise ValueError("Acquisition ledger identity/settings mismatch")
            if row is None:
                with closing(sqlite3.connect(self.journal.database_path.resolve().as_uri() + "?mode=ro", uri=True)) as j:
                    clause='';params=(self.settings.channel.value,)
                    if getattr(self,'history_scope',None) is not None:
                        from .quote_observation_store import _utc_text
                        clause=' AND scheduled_at_utc>=? AND scheduled_at_utc<?'
                        params+=tuple(_utc_text(at,'history_scope')for at in self.history_scope)
                    count = j.execute("SELECT count(*) FROM quote_acquisition WHERE channel=?"+clause, params).fetchone()[0]
                    count += j.execute("SELECT count(*) FROM poll_slot_skip WHERE channel=?"+clause, params).fetchone()[0]
                if count:
                    raise ValueError("Restore original acquisition ledger; channel already has outcomes")
                c.execute("INSERT INTO acquisition_identity VALUES(1,?)", (canonical(self.identity),))
            c.execute("CREATE TABLE IF NOT EXISTS acquisition_event(sequence INTEGER PRIMARY KEY,payload TEXT NOT NULL,previous_sha256 TEXT NOT NULL,event_sha256 TEXT NOT NULL)")
        self.journal.record_run(self.run)
        self.snapshot()

    def _request(self, p):
        slot = ExplicitPollSlot(self.settings.channel, stamp(p["scheduled_at_utc"]), self.settings.seconds)
        return PollRequest(slot, p["revision"], tuple(p["symbols"]), stamp(p["dispatched_at_utc"]))

    def _skip(self, p):
        slot = ExplicitPollSlot(self.settings.channel, stamp(p["scheduled_at_utc"]), self.settings.seconds)
        return SkippedPollSlot(slot, p["revision"], stamp(p["membership_effective_at_utc"]), stamp(p["dispatched_at_utc"]))

    def _receipt(self, pending, *, connection=None):
        if connection is None:
            with self.journal._connection() as connection:
                return self._receipt(pending, connection=connection)
        p = pending["dispatch"]
        if p["symbols"]:
            row = connection.execute("SELECT * FROM quote_acquisition WHERE acquisition_id=?", (self._request(p).batch_id,)).fetchone()
            a = self.journal._stored_acquisition(row) if row is not None else None
            if a is None:
                return None
            assembled = pending["assembled"]
            if assembled is None:
                raise ValueError("Journal acquisition exists without this ledger's assembled response")
            result = decode_result(assembled["result"])
            if (a.run_id != p["run_id"] or a.channel != self.settings.channel.value or a.channel_revision != p["revision"] or
                a.scheduled_at_utc != stamp(p["scheduled_at_utc"]) or a.dispatched_at_utc != stamp(p["dispatched_at_utc"]) or
                a.completed_at_utc != stamp(assembled["completed_at_utc"]) or a.request_count != result.request_count or
                a.batch_size != result.batch_size or a.requested_symbol_count != len(result.results) or a.unexpected_symbols != result.unexpected_symbols):
                raise ValueError("Journal acquisition header differs from assembled response")
            columns = ("acquisition_id", "symbol", "ordinal", "status", "detail", "schwab_batch_number",
                       "request_started_at_utc", "response_received_at_utc", "normalized_schema_version", *OBSERVATION_COLUMNS)
            rows = [tuple(row) for row in connection.execute("SELECT " + ",".join(columns) + " FROM quote_observation WHERE acquisition_id=? ORDER BY ordinal", (a.acquisition_id,))]
            key = (a.acquisition_id, pending.get("assembled_event_sha256"))
            expected = self._normalization_cache.get(key) if key[1] is not None else None
            if expected is None:
                normalization_started = time.perf_counter()
                expected = tuple(self.journal._observation_values(a.acquisition_id, n, r) for n, r in enumerate(result.results))
                self.normalization_seconds += time.perf_counter() - normalization_started
                self.normalization_cache_misses += 1
                if key[1] is not None:
                    self._normalization_cache[key] = expected
                    if len(self._normalization_cache) > 1024:
                        self._normalization_cache.popitem(last=False)
            else:
                self.normalization_cache_hits += 1
                self._normalization_cache.move_to_end(key)
            if tuple(rows) != expected:
                raise ValueError("Journal observations differ from assembled response")
            return {"kind": "acquisition", "id": a.acquisition_id, "content_sha256": a.content_sha256}
        row = connection.execute("SELECT content_sha256 FROM poll_slot_skip WHERE slot_id=?", (p["slot_id"],)).fetchone()
        return {"kind": "empty_membership", "id": p["slot_id"], "content_sha256": row[0]} if row else None

    def _read(self):
        started = time.perf_counter()
        try:
            with self.journal._connection() as connection:
                return self._read_checked(connection)
        finally:
            self.verification_seconds += time.perf_counter() - started
            self.verification_reads += 1

    def _read_checked(self, connection):
        if self.settings.payload() != self.identity["settings"] or str(self.journal.database_path.resolve()) != self.identity["journal"]:
            raise ValueError("Acquisition configuration changed")
        revisions = self.journal.membership_revisions_in_effective_order()
        if not revisions or revisions[0].content_sha256 != self.identity["baseline_sha256"]:
            raise ValueError("Acquisition journal baseline changed")
        effective_times = tuple(r.effective_at for r in revisions)
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as c:
            row = c.execute("SELECT payload FROM acquisition_identity WHERE id=1").fetchone()
            if row is None or row[0] != canonical(self.identity):
                raise ValueError("Acquisition ledger identity/settings mismatch")
            rows = list(c.execute("SELECT * FROM acquisition_event ORDER BY sequence"))
        previous, events, pending, finished, last_time = digest(self.identity), [], None, {}, None
        for sequence, raw, prior, sha in rows:
            e = json.loads(raw)
            if sequence != len(events) + 1 or prior != previous or sha != digest({"sequence": sequence, "payload": e, "previous_sha256": prior}):
                raise ValueError("Acquisition outcome chain differs")
            at = stamp(e["recorded_at_utc"])
            if last_time is not None and at < last_time:
                raise ValueError("Acquisition outcome clock reversed")
            last_time = at
            kind = e["kind"]
            if kind == "dispatch":
                if pending or e["slot_id"] in finished:
                    raise ValueError("Duplicate or overlapping acquisition dispatch")
                index = bisect_right(effective_times, stamp(e["scheduled_at_utc"])) - 1
                revision = revisions[index] if index >= 0 else None
                if revision is None or revision.revision != e["revision"] or revision.content_sha256 != e["hierarchy_sha256"] or list(revision.symbols_for(self.settings.channel)) != e["symbols"]:
                    raise ValueError("Acquisition dispatch membership differs")
                slot = ExplicitPollSlot(self.settings.channel, stamp(e["scheduled_at_utc"]), self.settings.seconds)
                if slot.slot_id != e["slot_id"] or not self.settings.window.contains(slot.scheduled_at) or stamp(e["dispatched_at_utc"]) < slot.scheduled_at:
                    raise ValueError("Acquisition dispatch slot differs")
                pending = {"dispatch": e, "assembled": None}
            elif kind in ("assembled", "acknowledged", "abandoned", "write_failed"):
                if pending is None or e["slot_id"] != pending["dispatch"]["slot_id"]:
                    raise ValueError("Acquisition outcome has no matching dispatch")
                if kind == "assembled":
                    if pending["assembled"] is not None or not pending["dispatch"]["symbols"]:
                        raise ValueError("Unexpected assembled acquisition")
                    result = decode_result(e["result"])
                    if [r.symbol for r in result.results] != pending["dispatch"]["symbols"]:
                        raise ValueError("Assembled symbols differ from dispatch")
                    dispatched, completed = stamp(pending["dispatch"]["dispatched_at_utc"]), stamp(e["completed_at_utc"])
                    for r in result.results:
                        if not dispatched <= r.request_started_at_utc <= completed or (r.response_received_at_utc is not None and not r.request_started_at_utc <= r.response_received_at_utc <= completed):
                            raise ValueError("Assembled acquisition timing differs")
                    pending["assembled"] = e
                    pending["assembled_event_sha256"] = sha
                elif kind == "acknowledged":
                    if pending["dispatch"]["symbols"] and not pending["assembled"]:
                        raise ValueError("Acknowledged acquisition has no durable result")
                    if self._receipt(pending, connection=connection) != e["journal_receipt"]:
                        raise ValueError("Acquisition journal acknowledgement differs")
                    finished[e["slot_id"]] = e; pending = None
                elif kind == "abandoned":
                    if pending["assembled"] is not None:
                        raise ValueError("Cannot abandon an assembled acquisition")
                    finished[e["slot_id"]] = e; pending = None
            elif kind == "missed":
                if pending or e["slot_id"] in finished:
                    raise ValueError("Conflicting missed-slot outcome")
                finished[e["slot_id"]] = e
            else:
                raise ValueError("Unsupported acquisition outcome")
            previous = sha; events.append(dict(e, sequence=sequence, event_sha256=sha))
        known_acquisitions, known_skips = set(), set()
        for e in events:
            if e["kind"] == "acknowledged":
                receipt = e["journal_receipt"]
                (known_acquisitions if receipt["kind"] == "acquisition" else known_skips).add(receipt["id"])
        if pending:
            receipt = self._receipt(pending, connection=connection)
            if receipt:
                (known_acquisitions if receipt["kind"] == "acquisition" else known_skips).add(receipt["id"])
        with closing(sqlite3.connect(self.journal.database_path.resolve().as_uri() + "?mode=ro", uri=True)) as c:
            actual_acquisitions = {r[0] for r in c.execute("SELECT acquisition_id FROM quote_acquisition WHERE channel=?", (self.settings.channel.value,))}
            actual_skips = {r[0] for r in c.execute("SELECT slot_id FROM poll_slot_skip WHERE channel=?", (self.settings.channel.value,))}
        if actual_acquisitions != known_acquisitions or actual_skips != known_skips:
            raise ValueError("Journal channel outcomes differ from acquisition ledger")
        return {"events": events, "pending": pending, "finished": finished, "head_sha256": previous}

    def snapshot(self):
        with owner_lock(self.lock_path):
            return self._read()

    def _append(self, s, kind, **payload):
        at = aware(self.clock())
        if s["events"] and at < stamp(s["events"][-1]["recorded_at_utc"]):
            raise ValueError("Acquisition clock reversed")
        e = dict(payload, kind=kind, recorded_at_utc=text(at))
        sequence, previous = len(s["events"]) + 1, s["head_sha256"]
        sha = digest({"sequence": sequence, "payload": e, "previous_sha256": previous})
        with closing(sqlite3.connect(self.path, timeout=5)) as c, c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT sequence,event_sha256 FROM acquisition_event ORDER BY sequence DESC LIMIT 1").fetchone()
            if row != ((sequence - 1, previous) if sequence > 1 else None):
                raise ValueError("Acquisition head changed; expected=" + str((sequence - 1, previous) if sequence > 1 else None) + "; actual=" + str(row))
            c.execute("INSERT INTO acquisition_event VALUES(?,?,?,?)", (sequence, canonical(e), previous, sha))

    def _commit(self, s, *, fault_hook=None, recovered=False):
        pending = s["pending"]; p = pending["dispatch"]
        started = time.perf_counter()
        try:
            if p["symbols"]:
                a = pending["assembled"]
                self.journal.record_acquisition(p["run_id"], self._request(p), decode_result(a["result"]), completed_at=stamp(a["completed_at_utc"]))
            else:
                self.journal.record_skipped_slot(p["run_id"], self._skip(p))
        except sqlite3.OperationalError as exc:
            self._append(s, "write_failed", slot_id=p["slot_id"], error_type=type(exc).__name__, error=str(exc))
            raise
        journal_seconds = time.perf_counter() - started
        if fault_hook:
            fault_hook("after_journal_commit")
        receipt = self._receipt(pending)
        if receipt is None:
            raise ValueError("Journal did not retain acquisition outcome")
        self._append(s, "acknowledged", slot_id=p["slot_id"], journal_receipt=receipt,
                     journal_seconds=journal_seconds, recovered=recovered)
        if fault_hook:
            fault_hook("after_ack")
        return {"status": "acquired" if p["symbols"] else "empty_membership", "slot_id": p["slot_id"],
                "revision": p["revision"], "symbols": p["symbols"], "recovered": recovered,
                "journal_seconds": journal_seconds,
                "collector_seconds": pending["assembled"]["collector_seconds"] if pending["assembled"] else 0}

    def _recover(self, s):
        pending = s["pending"]
        if pending is None:
            return None
        if pending["assembled"] is not None or not pending["dispatch"]["symbols"]:
            return self._commit(s, recovered=True)
        self._append(s, "abandoned", slot_id=pending["dispatch"]["slot_id"], reason="dispatch_outcome_unknown_after_interruption_no_repeat_api_request")
        return {"status": "unresolved_dispatch", "slot_id": pending["dispatch"]["slot_id"]}

    def recover(self):
        with owner_lock(self.lock_path):
            return self._recover(self._read())

    def sample(self, slot, *, fault_hook=None):
        cycle_started = time.perf_counter()
        verification_before = self.verification_seconds
        with owner_lock(self.lock_path):
            s = self._read()
            self._recover(s); s = self._read()
            if slot.watchlist_kind != self.settings.channel or slot.seconds != self.settings.seconds or not self.settings.window.contains(slot.scheduled_at):
                raise ValueError("Sample slot differs from this acquisition configuration")
            if slot.slot_id in s["finished"]:
                return {"status": "already_finished", "slot_id": slot.slot_id}
            now = aware(self.clock())
            if now < slot.scheduled_at:
                raise ValueError("Cannot acquire a future slot")
            gate1 = (now - slot.scheduled_at).total_seconds()
            state_prepare_seconds = time.perf_counter() - cycle_started
            if gate1 > self.settings.max_dispatch_lateness_seconds or now >= self.settings.window.end_at:
                reason = "window_ended" if now >= self.settings.window.end_at else "before_dispatch_deadline"
                self._append(s, "missed", slot_id=slot.slot_id, scheduled_at_utc=text(slot.scheduled_at), reason="late_dispatch_no_catchup", gate=reason, lateness_seconds=gate1)
                return {"status": "missed", "slot_id": slot.slot_id, "gate": reason, "gate_lateness_seconds": gate1, "state_prepare_seconds": state_prepare_seconds}
            revision = self.journal.latest_membership_revision_effective_at(slot.scheduled_at)
            if revision is None:
                raise ValueError("No hierarchy is effective at the scheduled slot")
            symbols = list(revision.symbols_for(self.settings.channel))
            p = {"slot_id": slot.slot_id, "scheduled_at_utc": text(slot.scheduled_at),
                 "dispatched_at_utc": text(now), "revision": revision.revision,
                 "membership_effective_at_utc": text(revision.effective_at),
                 "hierarchy_sha256": revision.content_sha256, "symbols": symbols, "run_id": self.run.run_id}
            spool_started = time.perf_counter()
            self._append(s, "dispatch", **p)
            if fault_hook:
                fault_hook("after_dispatch")
            s = self._read()
            now = aware(self.clock())
            gate2 = (now - slot.scheduled_at).total_seconds()
            spool_and_check_seconds = time.perf_counter() - spool_started
            if gate2 > self.settings.max_dispatch_lateness_seconds or now >= self.settings.window.end_at:
                reason = "window_ended" if now >= self.settings.window.end_at else "before_api_deadline"
                self._append(s, "abandoned", slot_id=slot.slot_id, reason="dispatch_budget_passed_before_api", gate=reason, lateness_seconds=gate2)
                return {"status": "missed", "slot_id": slot.slot_id, "gate": reason, "gate_lateness_seconds": gate2, "state_prepare_seconds": state_prepare_seconds, "spool_and_check_seconds": spool_and_check_seconds}
            if symbols:
                started = time.perf_counter()
                result = fetch_quotes_batched(self.client, symbols, fields=self.settings.fields, batch_size=self.settings.batch_size)
                if fault_hook:
                    fault_hook("after_response")
                completed = aware(self.clock())
                encoded = encode_result(result)
                self._append(s, "assembled", slot_id=slot.slot_id, completed_at_utc=text(completed),
                             collector_seconds=time.perf_counter() - started, result=encoded)
                if fault_hook:
                    fault_hook("after_assembled")
                s = self._read()
            outcome = self._commit(s, fault_hook=fault_hook)
            outcome["cycle_seconds"] = time.perf_counter() - cycle_started
            outcome["verification_seconds"] = self.verification_seconds - verification_before
            outcome.update(state_prepare_seconds=state_prepare_seconds, spool_and_check_seconds=spool_and_check_seconds,
                before_dispatch_gate_seconds=gate1, before_api_gate_seconds=gate2,
                recovery_before_slot_seconds=getattr(self, 'last_recovery_seconds', 0.0))
            outcome["dispatch_lateness_seconds"] = (stamp(p["dispatched_at_utc"]) - slot.scheduled_at).total_seconds()
            return outcome

    def run_loop(self, stop: Event, *, on_status=None):
        """No catch-up bursts. API timeout/cancellation belongs to the supplied client."""
        while not stop.is_set():
            try:
                result = self.recover()
                if result and on_status:
                    on_status(result)
                slot = next_slot(self.settings, after=aware(self.clock()))
                if slot is None:
                    return {"status": "window_ended"}
                if stop.wait(max(0, (slot.scheduled_at - self.clock()).total_seconds())):
                    break
                result = self.sample(slot)
            except (sqlite3.OperationalError, AcquisitionBusy) as exc:
                result = {"status": "retrying", "error_type": type(exc).__name__, "error": str(exc)}
                if on_status:
                    on_status(result)
                stop.wait(self.settings.retry_seconds)
                continue
            if on_status:
                on_status(result)
        return {"status": "stopped"}
