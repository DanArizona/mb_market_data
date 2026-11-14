"""Daily 09:25 current-volume selection and calendar-bound post-close handoff.

No credentials, process launching or secret persistence in this module. The
caller supplies one authenticated client. Runtime data never changes r0 inputs.
"""
from __future__ import annotations

import csv
from functools import lru_cache
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from importlib.metadata import version
import json
from pathlib import Path
import sqlite3
from contextlib import closing
from typing import Callable
from zoneinfo import ZoneInfo

from .opening_hierarchy import load_opening_proposal, write_opening_proposal
from .polling_cadence import INTERLEAVED, check_before_credentials
from .quote_observation_store import QuoteObservationStore
from .sampling_membership import SamplingHierarchyRevision
from .schwab_quotes import fetch_quotes_batched
from .volume_proxy_experiment import rank_values, volume_value, write_json, utc_text

ET = ZoneInfo('America/New_York')
METHOD = 'current-volume-0925-v1'
CONTRACT = 'one-unlock-daily-flow-v1'


@dataclass(frozen=True)
class SessionSchedule:
    day: date
    opens_at: datetime
    closes_at: datetime
    next_day: date
    calendar_version: str

    def payload(self):
        return {'session_date': str(self.day), 'opens_at': self.opens_at.isoformat(),
                'closes_at': self.closes_at.isoformat(), 'next_session': str(self.next_day),
                'calendar': 'XNYS', 'calendar_version': self.calendar_version}


@lru_cache(maxsize=32)
def schedule_for(day: date) -> SessionSchedule:
    """Read installed exchange calendar, not a live exchange-status feed."""
    import exchange_calendars as xcals
    cal = xcals.get_calendar('XNYS', start=f'{day.year-1}-12-01', end=f'{day.year+1}-02-01')
    if not cal.is_session(str(day)):
        raise ValueError(f'{day} is not a scheduled exchange session')
    start = cal.session_open(str(day)).to_pydatetime().astimezone(ET)
    stop = cal.session_close(str(day)).to_pydatetime().astimezone(ET)
    if start.date() != day or stop.date() != day or start.time() != time(9, 30):
        raise ValueError('Unexpected exchange session; review before launching')
    return SessionSchedule(day, start, stop, cal.next_session(str(day)).date(),
                           version('exchange-calendars'))


def journal_path(data_root: Path, day: date) -> Path:
    return data_root / 'output' / f'quote_observation_journal_v2_api_opening_{day}' / f'{day}.sqlite3'


def make_plan(opening_path: Path, data_root: Path, ecfg: Path, *, allow_carried=False,
              schedule_resolver=schedule_for) -> dict:
    opening_path, data_root, ecfg = map(lambda p: Path(p).expanduser().resolve(),
                                      (opening_path, data_root, ecfg))
    r0 = load_opening_proposal(opening_path)
    if not r0.uni_symbols:
        raise ValueError('Opening Uni is empty')
    if (r0.metadata.get('test_only') or r0.metadata.get('carry_forward') or
            'carried' in r0.source or 'carry-forward' in r0.source) and not allow_carried:
        raise ValueError('Carried/test roster requires explicit --allow-carried-roster; origin is preserved')
    schedule = schedule_resolver(r0.session_date)
    if r0.effective_at != schedule.opens_at:
        raise ValueError('Opening r0 effective time differs from exchange open')
    if not data_root.is_dir():
        raise ValueError('Explicit existing market-data root required')
    database = journal_path(data_root, r0.session_date)
    check_before_credentials(database, INTERLEAVED)
    return {'contract': CONTRACT, 'method': METHOD, 'opening_path': str(opening_path),
            'opening_content_sha256': r0.content_sha256,
            'opening_metadata': json.loads(json.dumps(dict(r0.metadata), default=dict)),
            'data_root': str(data_root), 'ecfg': str(ecfg),
            'journal': str(database), 'state_root': str(data_root/'output'/'daily_session'/str(r0.session_date)),
            **schedule.payload(), 'snapshot_at': datetime.combine(r0.session_date,time(9,25),ET).isoformat(),
            'selection_deadline': datetime.combine(r0.session_date,time(9,29),ET).isoformat(),
            'focus_limit': 40, 'history_top_n': 100, 'batch_size': 400,
            'cadence_profile': INTERLEAVED, 'hot_activated': False,
            'postclose_delay_seconds': 60, 'postclose_window_seconds': 900,
            'allow_carried_roster': bool(allow_carried)}


def validate_start(plan: dict, *, now: datetime, resume=False) -> None:
    if plan.get('contract') != CONTRACT or plan.get('cadence_profile') != INTERLEAVED:
        raise ValueError('An explicit interleaved daily-flow plan is required')
    r0 = load_opening_proposal(plan['opening_path'])
    if r0.content_sha256 != plan['opening_content_sha256']:
        raise ValueError('Opening r0 changed since plan creation')
    if now.astimezone(ET).date() != r0.session_date:
        raise ValueError('Run on the opening r0 Eastern trading date')
    if not resume and now >= datetime.fromisoformat(plan['selection_deadline']):
        raise ValueError('Fresh selection cannot start at/after 09:29 ET; resume only uses a saved ranking')
    if now >= datetime.fromisoformat(plan['closes_at']) + timedelta(seconds=plan['postclose_window_seconds']):
        raise ValueError('The daily operating window has ended')
    check_before_credentials(Path(plan['journal']), INTERLEAVED)


def ensure_opening(plan: dict, *, resume=False) -> None:
    """Never replace an existing roster, cadence, or independently running session."""
    database = Path(plan['journal'])
    check_before_credentials(database, INTERLEAVED)
    if database.exists() and not resume:
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as con:
            if con.execute('SELECT count(*) FROM poll_run').fetchone()[0]:
                raise ValueError('This journal already has polling runs; do not attach a fresh launcher')
            if con.execute('SELECT count(*) FROM sampling_membership_revision WHERE revision>0').fetchone()[0]:
                raise ValueError('This journal already has r1/later membership; do not rerank it')
    r0 = load_opening_proposal(plan['opening_path'])
    store = QuoteObservationStore(database, session_date=r0.session_date, schema_version=2)
    store.initialize()
    store.record_membership_revision(r0)


def revision_payload(r):
    from .opening_hierarchy import _plain_json
    return {'session_date': str(r.session_date), 'revision': r.revision,
            'effective_at': r.effective_at.isoformat(), 'uni_symbols': list(r.uni_symbols),
            'focus_symbols': list(r.focus_symbols), 'hot_symbols': list(r.hot_symbols),
            'source': r.source, 'reason': r.reason, 'metadata': _plain_json(r.metadata)}


def read_revision(path: Path):
    value=json.loads(Path(path).read_text(encoding='utf-8'))
    return SamplingHierarchyRevision(**dict(value,session_date=date.fromisoformat(value['session_date']),
                       effective_at=datetime.fromisoformat(value['effective_at'])))


def acquire_selection(client, plan: dict, *, output: Path, clock=lambda: datetime.now(ET),
                      sleep: Callable[[float],None], stop_requested=lambda:False) -> dict:
    """All valid results retained. Retry missing symbols once; never zero-fill.

    Each attempt has its own raw timestamps. A recovered value remains visibly
    later, not silently relabelled as an exact 09:25 observation. No candles.
    """
    opening=load_opening_proposal(plan['opening_path'])
    if opening.content_sha256 != plan['opening_content_sha256']:
        raise ValueError('Opening roster changed')
    output=Path(output)
    output.mkdir(parents=True,exist_ok=False)
    scheduled=datetime.fromisoformat(plan['snapshot_at'])
    deadline=datetime.fromisoformat(plan['selection_deadline'])
    while clock()<scheduled:
        if stop_requested(): raise InterruptedError('Selection cancelled')
        sleep(min(0.25,(scheduled-clock()).total_seconds()))
    if clock()>=deadline:
        raise ValueError('No pre-open snapshot acquired before selection deadline')
    started=clock()
    values={s:None for s in opening.uni_symbols}
    accepted_rows={}; attempts=[]; request_count=0
    for attempt in (1,2):
        todo=[s for s in opening.uni_symbols if values[s] is None]
        if not todo or stop_requested() or clock()>=deadline: break
        # Bound starting each batch separately, not just the first request.
        for index in range(0,len(todo),400):
            if stop_requested() or clock()>=deadline: break
            names=todo[index:index+400]
            batch=fetch_quotes_batched(client,names,fields='all',batch_size=400)
            request_count+=batch.request_count
            for row in batch.results:
                q=row.quote or {}; field=q.get('quote') or {}
                value=volume_value(field.get('totalVolume')) if row.usable else None
                received=row.response_received_at_utc
                in_time=received is not None and received < deadline
                saved={'symbol':row.symbol,'attempt':attempt,'batch_index':request_count,
                       'status':row.status.value,'current_volume':value,
                       'eligible_for_selection':bool(value is not None and in_time),
                       'request_started_at_utc':utc_text(row.request_started_at_utc),
                       'response_received_at_utc':utc_text(received),'quote':q}
                attempts.append(saved)
                if value is not None and in_time:
                    values[row.symbol]=value; accepted_rows[row.symbol]=saved
            # Preserve each completed batch even if a later request hangs and
            # the controller terminates the selection child at its deadline.
            write_json(output/f'batch-{request_count:03d}.json',
                {'attempt':attempt,'requested_symbols':names,
                 'rows':[r for r in attempts if r['batch_index']==request_count]})
        if any(v is None for v in values.values()) and attempt==1 and clock()<deadline:
            sleep(min(1.0,(deadline-clock()).total_seconds()))
    ranking=rank_values(values)
    missing=sorted(s for s,v in values.items() if v is None)
    selected=list(ranking[:plan['focus_limit']])
    finished=clock()
    report={'contract':METHOD,'session_date':str(opening.session_date),
            'opening_content_sha256':opening.content_sha256,
            'opening_metadata':plan['opening_metadata'], 'scheduled_at':scheduled.isoformat(),
            'started_at':started.isoformat(),'completed_at':finished.isoformat(),
            'start_lateness_seconds':max(0,(started-scheduled).total_seconds()),
            'quote_requests':request_count,'history_requests':0,'elapsed_seconds':(finished-started).total_seconds(),
            'requested_symbols':len(values),'valid_symbols':len(ranking),'missing_symbols':missing,
            'selected_focus':selected,'top_100':list(ranking[:100]),
            'selection_policy':'CURRENT_VOLUME_DESC_SYMBOL_ASC',
            'status':'READY' if not missing else 'DEGRADED' if selected else 'FOCUS_UNAVAILABLE',
            'membership_published':False, 'rows':attempts}
    write_json(output/'snapshot.json',report)
    fields=['rank','symbol','current_volume','in_focus','in_top_100','status',
            'attempt','response_received_at_utc']
    order=list(ranking)+missing
    for filename, symbols in [('ranking.csv',order),('top100.csv',list(ranking[:100]))]:
        with (output/filename).open('x',encoding='utf-8',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
            ranks={s:n for n,s in enumerate(ranking,1)}
            for s in symbols:
                row=accepted_rows.get(s,{})
                writer.writerow({'rank':ranks.get(s),'symbol':s,'current_volume':values[s],
                    'in_focus':s in selected,'in_top_100':s in ranking[:100],
                    'status':'available' if s in ranks else 'missing',
                    'attempt':row.get('attempt'),'response_received_at_utc':row.get('response_received_at_utc')})
    # Do not claim a proposed r1 if no value exists or selection crossed the open.
    if selected and not stop_requested() and finished < opening.effective_at:
        r1=SamplingHierarchyRevision(session_date=opening.session_date,revision=1,
            effective_at=opening.effective_at,uni_symbols=opening.uni_symbols,
            focus_symbols=tuple(selected),hot_symbols=(),source=METHOD,
            reason='opening Focus from current-volume ranking at 09:25 ET',
            metadata={'selection_method':METHOD,'ranked_focus_symbols':selected,
                      'ranking_snapshot':str(output/'snapshot.json'),
                      'opening_content_sha256':opening.content_sha256,
                      'missing_symbols':missing,'requested_top_n':40,
                      'started_at':started.isoformat(),'completed_at':finished.isoformat()})
        write_json(output/'r1.json',revision_payload(r1))
        report['proposal']=str(output/'r1.json')
    write_json(output/'result.json',{k:v for k,v in report.items() if k!='rows'})
    return report


def publish_selection(plan, result, *, now):
    """Publish exactly the saved ranked selection. No later-time reranking."""
    if not result.get('proposal'): return False
    if now >= datetime.fromisoformat(plan['opens_at']):
        raise ValueError('Opening already passed; preserve snapshot but do not backdate r1')
    r1=read_revision(Path(result['proposal']))
    r0=load_opening_proposal(plan['opening_path'])
    if (r1.session_date!=r0.session_date or r1.revision!=1 or r1.uni_symbols!=r0.uni_symbols
            or r1.metadata.get('opening_content_sha256')!=r0.content_sha256
            or set(r1.focus_symbols)!=set(result['selected_focus']) or r1.source!=METHOD):
        raise ValueError('Selection proposal does not match the original roster and result')
    store=QuoteObservationStore(plan['journal'],session_date=r0.session_date,schema_version=2)
    store.record_membership_revision(r1)
    from .acquisition_health import atomic_json
    atomic_json(Path(result['proposal']).parent/'publication.json', {
        'status':'PUBLISHED','journal':plan['journal'],'revision':1,
        'content_sha256':r1.content_sha256,'effective_at':r1.effective_at.isoformat(),
        'confirmed_at':now.isoformat(),'selection_method':METHOD})
    return True


def build_next_opening(client, plan, *, clock=lambda:datetime.now(ET), builder=None):
    """Independent of Uni health. A failed publication never reports READY."""
    from .postclose_universe import build_with_client
    builder=builder or build_with_client
    day=date.fromisoformat(plan['session_date']); target=date.fromisoformat(plan['next_session'])
    closes=datetime.fromisoformat(plan['closes_at'])
    if clock().astimezone(ET).date()!=day or clock()<closes:
        raise ValueError('Post-close snapshot requires same source date after the scheduled close')
    output=Path(plan['data_root'])/'output'/'daily_universe_production'/f'{target}-from-{day}'
    if output.exists():
        # A complete prior build can be published/reconfirmed without another
        # snapshot. Partial evidence is not overwritten or silently adopted.
        from .daily_universe import sha256_file
        manifest=json.loads((output/'workflow_manifest.json').read_text(encoding='utf-8'))
        if manifest['session_date']!=str(day) or manifest['target_date']!=str(target):
            raise ValueError('Existing build dates differ')
        for artifact in manifest['artifacts'].values():
            path=(output/artifact['path']).resolve()
            if not path.is_relative_to(output.resolve()) or sha256_file(path)!=artifact['sha256']:
                raise ValueError('Existing build artifact differs')
        snapshot=json.loads((output/'market_snapshot'/'manifest.json').read_text(encoding='utf-8'))
        if any(k not in ('quote','invalid') and n for k,n in snapshot['acquisition']['status_counts'].items()):
            raise ValueError('Existing broad snapshot has unavailable transport results')
        result=json.loads((output/'postclose_result.json').read_text(encoding='utf-8'))
        if Path(result['opening_proposal']).resolve()!=output/'opening_hierarchy_r0.json':
            raise ValueError('Existing build points outside its own r0')
        result['reused_complete_build']=True
    else:
        result=builder(client,session_date=day,target_date=target,output_dir=output,clock=clock,
                       exchange_close=closes,require_complete_transport=True,
                       opening_focus_policy='empty-awaiting-current-volume-0925')
    r0=load_opening_proposal(result['opening_proposal'])
    if r0.session_date!=target:raise ValueError('Built r0 has wrong target date')
    result=dict(result,status='BUILT_PUBLICATION_PENDING',membership_published=False)
    from .acquisition_health import atomic_json
    atomic_json(output/'daily_handoff.json',result)
    store=QuoteObservationStore(journal_path(Path(plan['data_root']),target),session_date=target,schema_version=2)
    store.initialize();store.record_membership_revision(r0)
    result.update(status='READY',membership_published=True)
    atomic_json(output/'daily_handoff.json',result)
    return result
