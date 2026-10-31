"""Derived in-memory verification cache for the daily acquisition reader.

Every read still compares all persisted event/header/observation/skip values.
Only already-verified parsing, hashing and normalization work is reused. No
mtime/size/head-only check or persistent trust cache is used.
"""
from bisect import bisect_right
from contextlib import closing
import json
import hashlib
import sqlite3
from types import MappingProxyType

from .sampling_acquisition import canonical, digest, stamp, ExplicitPollSlot, decode_result, OBSERVATION_COLUMNS
_SCALARS=frozenset((str,int,float,bool,type(None)))


class FrozenList(tuple):
    """Immutable cached sequence retaining the reader's list-value comparisons."""
    def __eq__(self,other):
        if isinstance(other,(list,tuple)):return tuple(self)==tuple(other)
        return NotImplemented
    def __ne__(self,other):
        equal=self.__eq__(other)
        return NotImplemented if equal is NotImplemented else not equal
    __hash__=None


def immutable(value):
    if type(value) in _SCALARS:return value
    if isinstance(value,MappingProxyType): return value
    if isinstance(value,FrozenList):return value
    if isinstance(value,dict): return MappingProxyType({k:immutable(v) for k,v in value.items()})
    if isinstance(value,(list,tuple)): return FrozenList(immutable(v) for v in value)
    return value


def mutable(value):
    from collections.abc import Mapping
    if isinstance(value,Mapping):return {k:mutable(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [mutable(v) for v in value]
    return value


def _freeze_json_list(value):
    # JSON object hooks have already frozen every nested dictionary. Only
    # arrays need conversion here, including arrays nested inside arrays.
    return FrozenList(_freeze_json_list(item) if type(item) is list else item for item in value)


def _freeze_json_object(value):
    for key,item in value.items():
        if type(item) is list:value[key]=_freeze_json_list(item)
    return MappingProxyType(value)


def read_daily_history(self,connection):
    from .daily_raw_history import RawHistoryMirror
    if self._raw_history is None:self._raw_history=RawHistoryMirror(self)
    raw=self._raw_history.read(canonical(self.identity))
    headers,observations,skips=raw['headers'],raw['observations'],raw['skips']
    grouped={}
    old_count=len(self._verified_history['raw_observations']) if self._verified_history is not None else 0
    for row in observations[old_count:]:grouped.setdefault(row[0],[]).append(row)
    self._reader_view={'headers':{row['acquisition_id']:row for row in headers},
        'observations':grouped,'skips':{row['slot_id']:row for row in skips},
        'raw_headers':raw['raw_headers'],'raw_observations':observations,
        'raw_skips':raw['raw_skips'],'ledger_rows':raw['events']}
    try:return read_verified_history(self,connection)
    finally:self._reader_view=None


def receipt_from_view(self,pending,view):
    p=pending['dispatch']
    if not p['symbols']:
        row=view['skips'].get(p['slot_id'])
        return {'kind':'empty_membership','id':p['slot_id'],'content_sha256':row['content_sha256']} if row else None
    row=view['headers'].get(self._request(p).batch_id)
    if row is None:return None
    a=self.journal._stored_acquisition(row)
    assembled=pending['assembled']
    if assembled is None:raise ValueError('Journal acquisition exists without this ledger\'s assembled response')
    result=decode_result(assembled['result'])
    if (a.run_id!=p['run_id'] or a.channel!=self.settings.channel.value or a.channel_revision!=p['revision']
            or a.scheduled_at_utc!=stamp(p['scheduled_at_utc']) or a.dispatched_at_utc!=stamp(p['dispatched_at_utc'])
            or a.completed_at_utc!=stamp(assembled['completed_at_utc']) or a.request_count!=result.request_count
            or a.batch_size!=result.batch_size or a.requested_symbol_count!=len(result.results)
            or a.unexpected_symbols!=result.unexpected_symbols):
        raise ValueError('Journal acquisition header differs from assembled response')
    expected=tuple(self.journal._observation_values(a.acquisition_id,n,r) for n,r in enumerate(result.results))
    actual=view['observations'].get(a.acquisition_id)
    if actual is None:
        # A previously committed, still-pending response may precede the new
        # tail. Its bytes were compared above; recover it without rebuilding
        # thousands of unrelated per-acquisition lists on every warm read.
        actual=tuple(row for row in view['raw_observations'] if row[0]==a.acquisition_id)
    if tuple(actual)!=expected:
        raise ValueError('Journal observations differ from assembled response')
    return {'kind':'acquisition','id':a.acquisition_id,'content_sha256':a.content_sha256}


def read_verified_history(self,connection):
    if self.settings.payload() != self.identity["settings"] or str(self.journal.database_path.resolve()) != self.identity["journal"]:
        raise ValueError("Acquisition configuration changed")
    revisions = self.journal.membership_revisions_in_effective_order()
    if not revisions or revisions[0].content_sha256 != self.identity["baseline_sha256"]:
        raise ValueError("Acquisition journal baseline changed")
    effective_times = tuple(r.effective_at for r in revisions)
    rows=self._reader_view['ledger_rows']
    cache=self._verified_history
    consumed=0
    previous, events, pending, finished, last_time = digest(self.identity), [], None, {}, None
    revision_key=tuple((r.revision,r.effective_at,r.content_sha256,tuple(r.symbols_for(self.settings.channel))) for r in revisions)
    if cache is not None:
        consumed=len(cache['rows'])
        if tuple(rows[:consumed])!=cache['rows']:
            raise ValueError('Previously verified acquisition prefix changed')
        view=self._reader_view
        for name in ('raw_headers','raw_observations','raw_skips'):
            old=cache[name]
            if view[name][:len(old)]!=old:
                raise ValueError('Previously verified journal values changed')
        old_ids=cache['acquisition_ids']
        if any(r[0] in old_ids for r in view['raw_observations'][len(cache['raw_observations']):]):
            raise ValueError('Extra observation appended to an already verified acquisition')
        if revision_key!=cache['revision_key']:
            for e in cache['state']['events']:
                if e['kind']!='dispatch':continue
                index=bisect_right(effective_times,stamp(e['scheduled_at_utc']))-1
                revision=revisions[index] if index>=0 else None
                if (revision is None or revision.revision!=e['revision'] or revision.content_sha256!=e['hierarchy_sha256']
                        or tuple(revision.symbols_for(self.settings.channel))!=tuple(e['symbols'])):
                    raise ValueError('Acquisition dispatch membership differs')
        state=cache['state']
        previous=state['head_sha256'];events=list(state['events']);pending=mutable(state['pending'])
        finished=dict(state['finished'])
        last_time=stamp(events[-1]['recorded_at_utc']) if events else None
        self.history_reused_events+=consumed
    for sequence, raw, prior, sha in rows[consumed:]:
        e = json.loads(raw,object_hook=_freeze_json_object)
        # V2 binds the exact JSON bytes emitted by the canonical native writer.
        # Parsing still validates the event semantics below; encoding that same
        # payload again is unnecessary for its chain digest. Formatting-only
        # edits therefore cannot retain the old digest, even on a cold read.
        envelope='{"payload":'+raw+',"previous_sha256":"'+prior+'","sequence":'+str(sequence)+'}'
        expected=hashlib.sha256(envelope.encode('utf-8')).hexdigest()
        if sequence != len(events) + 1 or prior != previous or sha != expected:
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
        previous = sha; events.append(MappingProxyType(dict(e, sequence=sequence, event_sha256=sha)))
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
        clause='';params=(self.settings.channel.value,)
        if getattr(self,'history_scope',None) is not None:
            from .quote_observation_store import _utc_text
            clause=' AND scheduled_at_utc>=? AND scheduled_at_utc<?'
            params+=tuple(_utc_text(at,'history_scope')for at in self.history_scope)
        actual_acquisitions = {r[0] for r in c.execute("SELECT acquisition_id FROM quote_acquisition WHERE channel=?"+clause, params)}
        actual_skips = {r[0] for r in c.execute("SELECT slot_id FROM poll_slot_skip WHERE channel=?"+clause, params)}
    if actual_acquisitions != known_acquisitions or actual_skips != known_skips:
        raise ValueError("Journal channel outcomes differ from acquisition ledger")
    state=immutable({"events": events, "pending": pending, "finished": finished, "head_sha256": previous})
    view=self._reader_view
    self._verified_history={'rows':tuple(rows),'state':state,'revision_key':revision_key,
        **{name:view[name] for name in ('raw_headers','raw_observations','raw_skips')},
        'acquisition_ids':frozenset(view['headers'])}
    return state

