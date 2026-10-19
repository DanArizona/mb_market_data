"""Opt-in guarded, incremental acquisition for fresh experimental journals.

SQLite guards protect committed prefixes from ordinary SQL modification. This
is not a security boundary against an administrator replacing files or forging
all metadata. Full reconstruction remains independent of the in-memory cursor.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import time
from contextlib import closing

from mb_market_data.sampling_acquisition import (
    IsolatedSamplingPoller, ExplicitPollSlot, canonical, digest, stamp, owner_lock, decode_result,
)

INCREMENTAL_MARKER = 'guarded_incremental_acquisition_v1'
JOURNAL_TABLES = ('store_identity','poll_run','sampling_membership_revision','sampling_channel_revision',
                  'sampling_channel_member','poll_slot_skip','quote_acquisition','quote_observation')
SIDECAR_TABLES = ('acquisition_identity','acquisition_event')


def quoted(name):
    return '"' + name.replace('"','""') + '"'


def guard_sql(connection, tables, *, journal):
    statements = {}
    for table in (*tables, 'ig_identity', *(('ig_changes',) if journal else ())):
        name = quoted(table)
        for operation in ('UPDATE','DELETE'):
            trigger = 'ig_' + table + '_' + operation.lower()
            statements[trigger] = f"CREATE TRIGGER {quoted(trigger)} BEFORE {operation} ON {name} BEGIN SELECT RAISE(ABORT,'incremental immutable history'); END"
        keys = []
        primary = [r[1] for r in sorted(connection.execute('PRAGMA table_info('+name+')'), key=lambda r:r[5]) if r[5]]
        if primary: keys.append(primary)
        for index in connection.execute('PRAGMA index_list('+name+')'):
            if index[2]:
                columns = [r[2] for r in connection.execute('PRAGMA index_info('+quoted(index[1])+')')]
                if columns not in keys:keys.append(columns)
        if not keys:raise ValueError('Guarded table requires explicit unique identity')
        clauses = ['EXISTS(SELECT 1 FROM '+name+' WHERE '+' AND '.join(quoted(c)+' IS NEW.'+quoted(c) for c in columns)+')' for columns in keys]
        trigger='ig_'+table+'_replace'
        statements[trigger]=f"CREATE TRIGGER {quoted(trigger)} BEFORE INSERT ON {name} WHEN {' OR '.join(clauses)} BEGIN SELECT RAISE(IGNORE); END" if table=='store_identity' else f"CREATE TRIGGER {quoted(trigger)} BEFORE INSERT ON {name} WHEN {' OR '.join(clauses)} BEGIN SELECT RAISE(ABORT,'incremental duplicate/replace refused'); END"
    if journal:
        bounds = {
            'quote_observation': "NOT EXISTS(SELECT 1 FROM quote_acquisition a WHERE a.acquisition_id=NEW.acquisition_id AND NEW.ordinal < a.requested_symbol_count AND NEW.ordinal >= 0)",
            'sampling_channel_member': "NOT EXISTS(SELECT 1 FROM sampling_channel_revision r WHERE r.channel=NEW.channel AND r.session_date=NEW.session_date AND r.revision=NEW.revision AND NEW.ordinal < r.symbol_count AND NEW.ordinal >= 0)",
        }
        for table,condition in bounds.items():
            trigger='ig_'+table+'_bounds'
            statements[trigger]=f"CREATE TRIGGER {quoted(trigger)} BEFORE INSERT ON {quoted(table)} WHEN {condition} BEGIN SELECT RAISE(ABORT,'incremental ordinal/parent guard'); END"
        for table,key in (('quote_acquisition','acquisition_id'),('poll_slot_skip','slot_id')):
            trigger='ig_'+table+'_append'
            statements[trigger]=f"CREATE TRIGGER {quoted(trigger)} AFTER INSERT ON {quoted(table)} BEGIN INSERT INTO ig_changes(channel,kind,record_key) VALUES(NEW.channel,'{table}',NEW.{key}); END"
    return statements


def install_guards(path, tables, *, journal):
    """Fresh marker journals only; an existing guard set must match, never repair it silently."""
    with closing(sqlite3.connect(path)) as c,c:
        c.execute('BEGIN IMMEDIATE')
        exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ig_identity'").fetchone()
        if not exists:
            history = 'quote_acquisition' if journal else 'acquisition_event'
            if c.execute('SELECT count(*) FROM '+history).fetchone()[0]:
                raise ValueError('Incremental guards require fresh history; no automatic migration')
            c.execute('CREATE TABLE ig_identity(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL)')
            if journal:
                c.execute('CREATE TABLE ig_changes(sequence INTEGER PRIMARY KEY,channel TEXT NOT NULL,kind TEXT NOT NULL,record_key TEXT NOT NULL)')
                c.execute('CREATE INDEX ig_changes_channel_sequence ON ig_changes(channel,sequence)')
        expected=guard_sql(c,tables,journal=journal)
        identity=canonical({'contract':INCREMENTAL_MARKER,'journal':journal,'triggers':expected})
        if not exists:
            c.execute('INSERT INTO ig_identity VALUES(1,?)',(identity,))
            for sql in expected.values():c.execute(sql)
        else:
            row=c.execute('SELECT payload FROM ig_identity WHERE id=1').fetchone()
            actual=dict(c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'ig_%'"))
            if row!=(identity,) or actual!=expected:
                raise ValueError('Incremental guard definitions changed or missing')
        return c.execute('PRAGMA schema_version').fetchone()[0], identity


class IncrementalSamplingPoller(IsolatedSamplingPoller):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        if self.journal.membership_revisions_in_effective_order()[0].metadata.get(INCREMENTAL_MARKER) is not True:
            raise ValueError('Explicit guarded incremental opening marker required')
        self._ready=False
        self._checkpoint=None
        self.incremental_events=0
        self.full_reconstructions=0
        self.last_recovery_seconds=0.0
        self.recovery_seconds_total=0.0

    def initialize(self):
        journal_guard=install_guards(self.journal.database_path,JOURNAL_TABLES,journal=True)
        super().initialize()
        sidecar_guard=install_guards(self.path,SIDECAR_TABLES,journal=False)
        self._guard_versions=(journal_guard[0],sidecar_guard[0])
        self._guard_identities=(journal_guard[1],sidecar_guard[1])
        self._file_ids=tuple((p.stat().st_dev,p.stat().st_ino) for p in (self.journal.database_path,self.path))
        self._ready=True
        self.snapshot()

    def _check_guards(self, journal, sidecar):
        files=tuple((p.stat().st_dev,p.stat().st_ino) for p in (self.journal.database_path,self.path))
        versions=tuple(c.execute('PRAGMA schema_version').fetchone()[0] for c in (journal,sidecar))
        identities=tuple(tuple(c.execute('SELECT payload FROM ig_identity WHERE id=1').fetchone() or ()) for c in (journal,sidecar))
        if files!=self._file_ids or versions!=self._guard_versions or identities!=tuple((v,) for v in self._guard_identities):
            raise ValueError('Incremental storage/schema guard changed; full review required; '
                + 'file_identity=' + str(files) + ' expected=' + str(self._file_ids)
                + '; schema=' + str(versions) + ' expected=' + str(self._guard_versions)
                + '; guard_identity=' + str(identities!=tuple((v,) for v in self._guard_identities)))
        if self.settings.payload()!=self.identity['settings'] or str(self.journal.database_path.resolve())!=self.identity['journal']:
            raise ValueError('Acquisition configuration changed')
        if sidecar.execute('SELECT payload FROM acquisition_identity WHERE id=1').fetchone()!=(canonical(self.identity),):
            raise ValueError('Acquisition identity changed')

    def snapshot(self):
        with owner_lock(self.lock_path):
            if self._ready:
                with self.journal._connection() as j,closing(sqlite3.connect(self.path)) as c:self._check_guards(j,c)
            if self._ready:
                with self.journal._connection() as j:
                    sequence=j.execute('SELECT coalesce(max(sequence),0) FROM ig_changes').fetchone()[0]
            value=super()._read()
            self.full_reconstructions+=1
            if self._ready:
                self._checkpoint={**value, 'events': [self._event_header(e) for e in value['events']],
                    'pending': copy.deepcopy(value['pending']), 'finished': copy.deepcopy(value['finished'])}
                self._journal_cursor=sequence
            return value

    @staticmethod
    def _event_header(event):
        # Keep response bodies on disk. Only the current pending response is needed
        # in memory; full snapshot/replay still reads every original body.
        return {key:event[key] for key in ('recorded_at_utc','sequence','event_sha256')}

    def _read(self):
        if not self._ready:return super()._read()
        started=time.perf_counter()
        try:
            with self.journal._connection() as j,closing(sqlite3.connect(self.path)) as c:
                j.execute('BEGIN')
                self._check_guards(j,c)
                s=self._checkpoint
                if s is None:raise ValueError('No verified incremental checkpoint')
                start=len(s['events'])
                rows=list(c.execute('SELECT * FROM acquisition_event WHERE sequence>? ORDER BY sequence',(start,)))
                allowed=set()
                pending=s['pending']
                if pending and (pending['assembled'] or not pending['dispatch']['symbols']):
                    p=pending['dispatch'];allowed.add(self._request(p).batch_id if p['symbols'] else p['slot_id'])
                for sequence,raw,prior,sha in rows:
                    e=json.loads(raw)
                    if sequence!=len(s['events'])+1 or prior!=s['head_sha256'] or sha!=digest({'sequence':sequence,'payload':e,'previous_sha256':prior}):
                        raise ValueError('Incremental acquisition chain differs')
                    if s['events'] and stamp(e['recorded_at_utc'])<stamp(s['events'][-1]['recorded_at_utc']):raise ValueError('Acquisition clock reversed')
                    kind=e['kind'];pending=s['pending']
                    if kind=='dispatch':
                        if pending or e['slot_id'] in s['finished']:raise ValueError('Overlapping/duplicate dispatch')
                        revision=self.journal.latest_membership_revision_effective_at(stamp(e['scheduled_at_utc']))
                        if revision is None or revision.revision!=e['revision'] or revision.content_sha256!=e['hierarchy_sha256'] or list(revision.symbols_for(self.settings.channel))!=e['symbols']:
                            raise ValueError('Acquisition dispatch membership differs')
                        slot=ExplicitPollSlot(self.settings.channel,stamp(e['scheduled_at_utc']),self.settings.seconds)
                        if slot.slot_id!=e['slot_id'] or not self.settings.window.contains(slot.scheduled_at) or stamp(e['dispatched_at_utc'])<slot.scheduled_at:
                            raise ValueError('Acquisition dispatch slot differs')
                        s['pending']={'dispatch':e,'assembled':None}
                    elif kind in ('assembled','acknowledged','abandoned','write_failed'):
                        if pending is None or e['slot_id']!=pending['dispatch']['slot_id']:raise ValueError('Acquisition outcome has no dispatch')
                        if kind=='assembled':
                            if pending['assembled'] is not None or not pending['dispatch']['symbols']:raise ValueError('Unexpected assembled response')
                            result=decode_result(e['result']);p=pending['dispatch']
                            if [r.symbol for r in result.results]!=p['symbols']:raise ValueError('Assembled symbols differ')
                            for r in result.results:
                                if not stamp(p['dispatched_at_utc'])<=r.request_started_at_utc<=stamp(e['completed_at_utc']) or (r.response_received_at_utc is not None and not r.request_started_at_utc<=r.response_received_at_utc<=stamp(e['completed_at_utc'])):
                                    raise ValueError('Assembled response timing differs')
                            pending['assembled']=e;pending['assembled_event_sha256']=sha
                        elif kind=='acknowledged':
                            if pending['dispatch']['symbols'] and not pending['assembled']:raise ValueError('Acknowledgement lacks response')
                            if self._receipt(pending,connection=j)!=e['journal_receipt']:raise ValueError('Acquisition acknowledgement differs')
                            allowed.add(e['journal_receipt']['id'])
                            s['finished'][e['slot_id']]=e;s['pending']=None
                        elif kind=='abandoned':
                            if pending['assembled'] is not None:raise ValueError('Cannot abandon saved response')
                            s['finished'][e['slot_id']]=e;s['pending']=None
                    elif kind=='missed':
                        if pending or e['slot_id'] in s['finished']:raise ValueError('Conflicting missed slot')
                        s['finished'][e['slot_id']]=e
                    else:raise ValueError('Unknown acquisition event')
                    s['events'].append(self._event_header(dict(e,sequence=sequence,event_sha256=sha)));s['head_sha256']=sha
                    self.incremental_events+=1
                pending=s['pending']
                if pending and (pending['assembled'] or not pending['dispatch']['symbols']):
                    receipt=self._receipt(pending,connection=j)
                    if receipt:allowed.add(receipt['id'])
                newest=j.execute('SELECT coalesce(max(sequence),0) FROM ig_changes').fetchone()[0]
                changes=list(j.execute('SELECT record_key FROM ig_changes WHERE channel=? AND sequence>? AND sequence<=? ORDER BY sequence',(self.settings.channel.value,self._journal_cursor,newest)))
                if any(row[0] not in allowed for row in changes):raise ValueError('Unexpected journal channel append; full review required')
                self._journal_cursor=newest
                return s
        except BaseException:
            # A partial reducer must never be reused after integrity/operational failure.
            self._checkpoint=None
            raise
        finally:
            self.verification_seconds+=time.perf_counter()-started
            self.verification_reads+=1

    def recover(self):
        started=time.perf_counter()
        try:
            if self._ready and self._checkpoint is None:self.snapshot()
            return super().recover()
        finally:
            self.last_recovery_seconds=time.perf_counter()-started
            self.recovery_seconds_total+=self.last_recovery_seconds
