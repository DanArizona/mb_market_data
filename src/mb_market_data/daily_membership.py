"""Shared local membership writer for manual controls and Nasdaq episodes.

The existing native store remains the only membership authority. Each accepted
command and its demotion/episode state commit in the SAME atomic hierarchy
revision. There is no direct roster editing by the UI and no second collector.
An ordinary demotion is a symbol-level override of existing demand, not a
permanent suppression: a later halt timestamp may elevate the symbol again.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Callable
from uuid import uuid4

from .acquisition_health import atomic_json, operation_lock
from .sampling_membership import SamplingHierarchyRevision
from .quote_observation_store import QuoteObservationStore
from .schwab_quotes import fetch_quotes_batched
from .daily_worker_liveness import WorkerControl

UTC = timezone.utc
CONTRACT = 'daily-shared-membership-v1'
LEVELS = ('uni', 'focus', 'hot')
SYMBOL = re.compile(r'[A-Z0-9][A-Z0-9./^\-]{0,19}\Z')
ID = re.compile(r'(?:manual|nasdaq)-[0-9a-f]{32,64}\Z')


def utcnow():
    return datetime.now(UTC)


def timestamp(text):
    result = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if result.utcoffset() is None:
        raise ValueError('Timestamp must include a timezone')
    return result.astimezone(UTC)


def normalized(symbol):
    result = str(symbol).strip().upper()
    if not SYMBOL.fullmatch(result):
        raise ValueError('Enter one canonical security symbol')
    return result


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def plain(value):
    from collections.abc import Mapping
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def configure(plan, *, nasdaq=False):
    """Opt in explicitly; retain the verified three-channel plan otherwise."""
    if plan.get('channels') != ['uni', 'focus', 'hot']:
        raise ValueError('Membership controls require the three-channel plan')
    return dict(plan, membership_control=CONTRACT, automatic_nasdaq=bool(nasdaq),
                membership_max_hot=40, membership_max_extra=40,
                membership_effective_lead_seconds=3, nasdaq_interval_seconds=60)


def latest_revision(plan):
    """Short read-only snapshot, including an already published future revision."""
    path = Path(plan['journal']).resolve()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=.5)) as c:
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        row = c.execute('SELECT * FROM sampling_membership_revision WHERE session_date=? ORDER BY revision DESC LIMIT 1',
                        (plan['session_date'],)).fetchone()
        if row is None:
            raise ValueError('No published membership exists')
        # Reuse the native complete-revision verifier; do not derive a roster
        # from the per-channel health counters or a possibly stale status file.
        store = QuoteObservationStore(path, session_date=date.fromisoformat(plan['session_date']), schema_version=2)
        return store._membership_revision_from_row(c, row)


class Rejected(ValueError):
    """Expected invalid, stale or capacity-limited command; collectors continue."""


class RetryLater(RuntimeError):
    """Transient lookup/publication problem; no success is recorded."""


def verify_identity(client, symbol, evidence_path):
    """Use the authenticated member worker, never the control/viewer process."""
    batch = fetch_quotes_batched(client, [symbol], fields='all', batch_size=400)
    rows = []
    for item in batch.results:
        q = item.quote or {}
        rows.append({'symbol': item.symbol, 'status': item.status.value,
                     'quote': q, 'requested_at': item.request_started_at_utc.isoformat(),
                     'received_at': item.response_received_at_utc.isoformat() if item.response_received_at_utc else None})
    atomic_json(evidence_path, {'requested_symbol': symbol, 'request_count': batch.request_count, 'rows': rows})
    if len(rows) != 1 or rows[0]['symbol'] != symbol:
        raise RetryLater('Identity response incomplete')
    row = rows[0]
    if row['status'] in ('request_error', 'missing', 'response_error'):
        raise RetryLater('Identity provider temporarily unavailable')
    q = row['quote']
    if row['status'] != 'quote' or q.get('symbol') != symbol or q.get('assetMainType') != 'EQUITY':
        raise Rejected('Exact equity identity was not confirmed')
    return True


class MembershipWriter:
    """One owned writer, using saved r1/r2 as its starting membership.

    This adapts the native publication contract to the simpler daily launcher;
    it does not start the older standalone Hot coordinator in parallel.
    """
    def __init__(self, plan, client, *, clock=utcnow, validate=verify_identity, can_publish=lambda:True):
        self.plan, self.client, self.clock, self.validate = plan, client, clock, validate
        self.can_publish = can_publish
        self.root = Path(plan['state_root'])/'membership'
        self.root.mkdir(parents=True, exist_ok=True)
        self.receipts = self.root/'receipts'; self.receipts.mkdir(exist_ok=True)
        self.proofs = self.root/'identity'; self.proofs.mkdir(exist_ok=True)
        self.store = QuoteObservationStore(plan['journal'], session_date=date.fromisoformat(plan['session_date']), schema_version=2)
        # Opening Uni is immutable ranking input. Hot test seeds are legitimate
        # extras and count against the same new-symbol capacity.
        from .daily_flow import read_revision
        opening = read_revision(Path(plan['opening_path']))
        self.baseline = set(opening.uni_symbols)
        self.revision = latest_revision(plan)
        if self.revision.revision < 1:
            raise RetryLater('Wait for opening Focus publication')
        self.state = plain(self.revision.metadata.get('daily_membership', {}))
        self.state.setdefault('demoted_at', {})
        self.state.setdefault('episodes', {})
        self.state.setdefault('commands', {})
        self.state.setdefault('validated', sorted(set(self.revision.uni_symbols)-self.baseline))
        self.state.setdefault('extra_ever', sorted(set(self.revision.uni_symbols)-self.baseline))
        self.state.setdefault('ranked_focus_symbols', list(self.revision.metadata.get('ranked_focus_symbols', ())))

    def refresh(self):
        revision = latest_revision(self.plan)
        if revision.content_sha256 != self.revision.content_sha256:
            # No silent overwriting of changes made by another publisher.
            raise RetryLater('Another membership publisher changed this session; inspect before continuing')

    def receipt(self, command_id):
        path = self.receipts/(command_id+'.json')
        return read(path) if path.is_file() else None

    def apply(self, command):
        """Apply one idempotent command; success means published, not yet effective."""
        command = dict(command)
        cid = command.get('id', '')
        if not ID.fullmatch(cid):
            raise Rejected('Invalid command identifier')
        digest = fingerprint(command)
        saved = self.receipt(cid)
        if saved:
            if saved['command_sha256'] != digest:
                raise Rejected('Command ID reused with different content')
            return saved
        observed = latest_revision(self.plan)
        if observed.content_sha256 != self.revision.content_sha256:
            committed_state = plain(observed.metadata.get('daily_membership', {}))
            own = committed_state.get('commands', {}).get(cid)
            if own and own.get('command_sha256') == digest:
                self.revision, self.state = observed, committed_state
            else:
                raise RetryLater('Another publisher changed the session; no changes overwritten')
        prior = self.state['commands'].get(cid)
        if prior:
            if prior['command_sha256'] != digest:
                raise Rejected('Committed command ID differs')
            atomic_json(self.receipts/(cid+'.json'), prior)
            return prior  # commit-before-receipt recovery, no extra provider call
        now = self.clock().astimezone(UTC)
        if not timestamp(self.plan['opens_at']) <= now < timestamp(self.plan['closes_at']):
            raise Rejected('Membership changes require the active collection window')
        symbol = normalized(command.get('symbol', ''))
        action, target = command.get('action'), command.get('target')
        source = command.get('source')
        if source not in ('manual', 'nasdaq') or not cid.startswith(source+'-'):
            raise Rejected('Unknown command source')
        if action not in ('promote', 'demote') or target not in LEVELS:
            raise Rejected('Supported actions: promote/demote to uni, focus or hot')
        if source == 'nasdaq' and (action != 'promote' or target != 'hot'):
            raise Rejected('Nasdaq can only request Hot promotion')
        if action == 'demote' and target == 'hot':
            raise Rejected('Demotion target is Uni or Focus')
        state = plain(self.state)
        episode = None
        suppressed = None
        if source == 'nasdaq':
            halted = timestamp(command['halted_at'])
            if halted.date() != timestamp(self.plan['opens_at']).date() or halted > now:
                raise Rejected('Halt time is outside the observed session')
            episode = symbol+'@'+halted.isoformat()
            previous = state['episodes'].get(episode)
            cutoff = state['demoted_at'].get(symbol)
            latest_halt = max((timestamp(k.split('@', 1)[1]) for k in state['episodes'] if k.startswith(symbol+'@')), default=None)
            if previous is not None:
                suppressed = 'Same halt episode already handled'
            elif cutoff and halted <= timestamp(cutoff):
                suppressed = 'Ordinary demotion overrides this existing halt episode'
            elif latest_halt is not None and halted <= latest_halt:
                suppressed = 'Out-of-order older halt cannot promote again'
        members = {c: set(self.revision.symbols_for(c)) for c in LEVELS}
        current_level = max((LEVELS.index(c) for c in LEVELS if symbol in members[c]), default=-1)
        desired = LEVELS.index(target)
        if action == 'demote' and (current_level < 0 or desired > current_level):
            raise Rejected('Demote requires current membership at this level or higher')
        if source == 'nasdaq' and suppressed:
            result = {'id':cid, 'command_sha256':digest, 'status':'IGNORED', 'message':suppressed,
                      'symbol':symbol, 'source':source, 'recorded_at':now.isoformat()}
            atomic_json(self.receipts/(cid+'.json'), result)
            return result
        new_symbol = symbol not in self.baseline and symbol not in state['extra_ever']
        if new_symbol and len(state['extra_ever']) >= self.plan.get('membership_max_extra', 40):
            raise Rejected('Session new-symbol capacity reached')
        if action == 'promote':
            if target == 'hot' and symbol not in members['hot'] and len(members['hot']) >= self.plan.get('membership_max_hot',40):
                raise Rejected('Active Hot capacity reached')
            if symbol not in self.baseline and symbol not in state['validated']:
                self.validate(self.client, symbol, self.proofs/(cid+'.json'))
                state['validated'].append(symbol)
            if new_symbol:
                state['extra_ever'].append(symbol)
            for level in LEVELS[:desired+1]:
                members[level].add(symbol)
        else:
            for level in LEVELS[desired+1:]:
                members[level].discard(symbol)
            for level in LEVELS[:desired+1]:
                members[level].add(symbol)
            # This suppresses existing episodes even if their first feed report
            # is delayed until after this operator action.
            state['demoted_at'][symbol] = now.isoformat()
        if episode:
            state['episodes'][episode] = {'command_id':cid, 'first_observed_at':now.isoformat()}
        # Quote identity lookup may have taken time. Choose a future effective
        # time AFTER that lookup; never backdate to the request/actual halt.
        at = self.clock().astimezone(UTC)
        effective = max(at+timedelta(seconds=self.plan.get('membership_effective_lead_seconds',3)),
                        self.revision.effective_at+timedelta(microseconds=1))
        if effective >= timestamp(self.plan['closes_at']):
            raise Rejected('Too late to publish a future-effective change in this session')
        result = {'id':cid, 'command_sha256':digest, 'status':'PUBLISHED_PENDING_EFFECTIVE',
                  'symbol':symbol, 'action':action, 'target':target, 'source':source,
                  'revision':self.revision.revision+1, 'effective_at':effective.isoformat(),
                  'recorded_at':at.isoformat()}
        state['commands'][cid] = dict(result)
        metadata = {'daily_membership':state, 'command':command,
                    'ranked_focus_symbols':state['ranked_focus_symbols'],
                    'opening_content_sha256':self.plan['opening_content_sha256']}
        rev = SamplingHierarchyRevision(session_date=date.fromisoformat(self.plan['session_date']),
            revision=self.revision.revision+1, effective_at=effective,
            uni_symbols=tuple(sorted(members['uni'])), focus_symbols=tuple(sorted(members['focus'])),
            hot_symbols=tuple(sorted(members['hot'])), source=source+'-daily-membership-v1',
            reason=('Ordinary symbol demotion' if action=='demote' else 'Symbol promotion')+': '+symbol+' -> '+target,
            metadata=metadata)
        if not self.can_publish():
            raise Rejected('Controller stopped or changed before publication')
        self.store.record_membership_revision(rev)
        # A crash here is recovered from the command recorded in the revision.
        self.revision = latest_revision(self.plan)
        self.state = state
        atomic_json(self.receipts/(cid+'.json'), result)
        return result


def submit(root, *, action, symbol, target, clock=utcnow):
    """Local operator request. No credential access and no journal write here."""
    root = Path(root).resolve(); plan = read(root/'plan.json')
    if plan.get('membership_control') != CONTRACT:
        raise ValueError('This session was not launched with membership controls')
    from .daily_runtime import owner_available
    if not owner_available(root) or (root/'stop.request').exists() or (root/'exit.request').exists():
        raise ValueError('No active collecting session; no request written')
    at = clock().astimezone(UTC)
    if not timestamp(plan['opens_at']) <= at < timestamp(plan['closes_at']):
        raise ValueError('Submit membership commands during the collection window')
    owner = read(root/'owner.json'); service = read(root/'membership'/'status.json')
    if service.get('owner_pid') != owner['pid'] or (at-timestamp(service['at'])).total_seconds()>30 or service['state'] not in ('RUNNING','DEGRADED'):
        raise ValueError('Membership worker unavailable; collectors may still be running')
    if action not in ('promote','demote') or target not in LEVELS or (action=='demote' and target=='hot'):
        raise ValueError('Select promote to Uni/Focus/Hot or demote to Uni/Focus')
    cid = 'manual-'+uuid4().hex
    command={'id':cid, 'source':'manual','action':action,'symbol':normalized(symbol),'target':target,
             'submitted_at':at.isoformat(),'owner_pid':owner['pid'],
             'opening_content_sha256':plan['opening_content_sha256']}
    inbox=root/'membership'/'inbox';inbox.mkdir(parents=True,exist_ok=True)
    atomic_json(inbox/(cid+'.json'),command)
    return command


def report_result(root, cid, *, clock=utcnow):
    if not ID.fullmatch(cid):raise ValueError('Invalid command ID')
    directory=Path(root)/'membership';path=directory/'receipts'/(cid+'.json')
    if not path.exists():
        return {'id':cid,'status':'PENDING' if (directory/'inbox'/(cid+'.json')).exists() else 'NOT_FOUND'}
    value=read(path)
    if value.get('status')=='PUBLISHED_PENDING_EFFECTIVE' and clock()>=timestamp(value['effective_at']):
        value=dict(value,status='EFFECTIVE_AS_PUBLISHED',note='A later command may have changed the current membership; use members to see it.')
    return value


def service(plan, client, *, owner_pid, stop, clock=utcnow, sleep=time.sleep):
    """Single serialization point. Feed acquisition never runs in this loop."""
    root=Path(plan['state_root'])/'membership';root.mkdir(exist_ok=True,parents=True)
    inbox=root/'inbox';inbox.mkdir(exist_ok=True); receipts=root/'receipts';receipts.mkdir(exist_ok=True)
    attempts={}; engine=None;last_identity=[0.0];last_result=None
    def paced_identity(client,symbol,path):
        remaining=last_identity[0]+1-time.monotonic()
        while remaining>0:
            if stop():raise Rejected('Session stopped during identity pacing')
            sleep(min(.25,remaining));remaining=last_identity[0]+1-time.monotonic()
        last_identity[0]=time.monotonic()
        return verify_identity(client,symbol,path)
    with operation_lock('daily-membership:'+str(Path(plan['journal']).resolve()).casefold()):
        while not stop() and clock()<timestamp(plan['closes_at']):
            at=clock();state='RUNNING';detail=None
            try:
                if engine is None:engine=MembershipWriter(plan,client,clock=clock,validate=paced_identity,can_publish=lambda:not stop() and not (Path(plan['state_root'])/'stop.request').exists())
                if at<timestamp(plan['opens_at']):
                    state='WAITING_FOR_OPEN'
                elif not (Path(plan['state_root'])/'stop.request').exists():
                    # Manual operator actions take precedence over source backlog.
                    files=sorted(inbox.glob('*.json'),key=lambda p:(not p.name.startswith('manual-'),p.stat().st_mtime_ns,p.name))
                    for path in files[:1]:
                        if stop():break
                        retry=attempts.get(path.stem,{'count':0,'after':0})
                        if time.monotonic()<retry['after']:continue
                        command=None
                        try:
                            command=read(path)
                            if path.stem!=command.get('id') or not ID.fullmatch(path.stem):raise Rejected('Invalid request file')
                            if command.get('owner_pid')!=owner_pid:raise Rejected('Request belongs to an obsolete controller invocation')
                            if command.get('opening_content_sha256')!=plan['opening_content_sha256']:raise Rejected('Request belongs to another session roster')
                            last_result=engine.apply(command)
                        except Rejected as error:
                            atomic_json(receipts/path.name,{'id':path.stem,'status':'REJECTED','message':str(error),
                                'at':clock().isoformat(),'command_sha256':fingerprint(command or {})})
                        except (OSError,sqlite3.Error,RetryLater,ValueError,RuntimeError) as error:
                            retry={'count':retry['count']+1,'after':time.monotonic()+30};attempts[path.stem]=retry
                            state='DEGRADED';detail=type(error).__name__
                            if retry['count']<3:continue
                            atomic_json(receipts/path.name,{'id':path.stem,'status':'FAILED','message':'Membership action failed after bounded retries; existing collection unchanged.',
                                'error_type':type(error).__name__,'at':clock().isoformat(),'command_sha256':fingerprint(command or {})})
                        path.unlink(missing_ok=True)
                        attempts.pop(path.stem,None)
                else:state='PAUSED'
            except (OSError,sqlite3.Error,ValueError,RetryLater) as error:
                state='WAITING_FOR_BASELINE' if engine is None else 'DEGRADED';detail=type(error).__name__
            atomic_json(root/'status.json',{'state':state,'at':clock().isoformat(),'pid':os.getpid(),'owner_pid':owner_pid,
                'detail':detail,'pending':len(list(inbox.glob('*.json'))),'latest_revision':engine.revision.revision if engine else None,'last_result':last_result})
            sleep(.25)
    atomic_json(root/'status.json',{'state':'STOPPED','at':clock().isoformat(),'pid':os.getpid(),'owner_pid':owner_pid})
    return 0


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    for name in ('promote','demote'):
        q=sub.add_parser(name);q.add_argument('--state-root',type=Path,required=True);q.add_argument('--symbol',required=True)
        q.add_argument('--to',dest='target',choices=LEVELS,required=True)
        q.add_argument('--wait-seconds',type=float,default=15,help='Wait for publication/effectiveness feedback; no new provider request from this command')
    q=sub.add_parser('result');q.add_argument('--state-root',type=Path,required=True);q.add_argument('--id',required=True)
    q=sub.add_parser('members');q.add_argument('--state-root',type=Path,required=True)
    a=p.parse_args(argv)
    if a.action in ('promote','demote'):
        if not 0<=a.wait_seconds<=60:raise ValueError('Wait must be 0 through 60 seconds')
        value=submit(a.state_root,action=a.action,symbol=a.symbol,target=a.target)
        print('REQUEST SAVED:',value['id'],'(not yet effective)',flush=True)
        deadline=time.monotonic()+a.wait_seconds
        while True:
            result=report_result(a.state_root,value['id'])
            if result['status'] not in ('PENDING','PUBLISHED_PENDING_EFFECTIVE') or time.monotonic()>=deadline:
                print(json.dumps(result,indent=2))
                if result['status'] in ('PENDING','PUBLISHED_PENDING_EFFECTIVE'):
                    print('Still pending. Do not resubmit blindly; use result with the displayed ID to check.')
                return 1 if result['status'] in ('FAILED','REJECTED','NOT_FOUND') else 0
            time.sleep(.2)
    elif a.action=='result':print(json.dumps(report_result(a.state_root,a.id),indent=2))
    else:
        plan=read(a.state_root/'plan.json');rev=latest_revision(plan)
        print(json.dumps({'revision':rev.revision,'effective_at':rev.effective_at.isoformat(),
            'pending_effective':utcnow()<rev.effective_at,'uni':list(rev.uni_symbols),
            'focus':list(rev.focus_symbols),'hot':list(rev.hot_symbols)},indent=2))
    return 0
