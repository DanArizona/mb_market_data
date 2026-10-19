"""Native synthetic history, cold process startup, elapsed tail and crash recovery.

This is an isolated review harness. It never installs source or opens production
journals. Logical prefill is explicitly separate from actual elapsed scheduling.
"""
from __future__ import annotations
from contextlib import closing
from datetime import datetime, timedelta, timezone
import gc
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from threading import Event, Thread
import time
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT/'experiments/combined_schwab_20261019'))
from provider_load import (append_line, bootstrap, deny_network, lines, read_json, require,
    text, utc_now, write_json, GRIDS)

CONTRACT = 'isolated-polling-history-v1'
CHANNELS = ('uni', 'focus', 'hot')
MODES = ('coordinator', *CHANNELS)


class Clock:
    def __init__(self, folder, config, *, fixed=None):
        self.folder, self.config, self.fixed, self.arm = Path(folder), config, fixed, None

    def __call__(self):
        if self.fixed is not None:
            return self.fixed
        path = self.folder/'ARM.json'
        if self.arm is None and path.is_file():
            value = read_json(path)
            require(set(value) == {'run_id','actual_utc','logical_utc'} and value['run_id'] == self.config['run_id'], 'Clock identity differs')
            actual, logical = datetime.fromisoformat(value['actual_utc']), datetime.fromisoformat(value['logical_utc'])
            require(actual.tzinfo is not None and logical == datetime.fromisoformat(self.config['tail_start'])-timedelta(seconds=5), 'Clock origin differs')
            self.arm = logical-actual
        return utc_now()+self.arm if self.arm is not None else datetime.fromisoformat(self.config['tail_start'])-timedelta(seconds=5)


def configuration(folder, profile):
    from mb_market_data.watchlist_polling import ET
    require(profile in ('verify', 'full'), 'Explicit bounded profile required')
    start = (utc_now()+timedelta(days=2)).astimezone(ET).replace(hour=9, minute=30, second=0, microsecond=0)
    minutes = 3 if profile == 'verify' else 390
    end = start+timedelta(minutes=minutes)
    return dict(contract=CONTRACT, run_id=folder.name, run_root=str(folder.resolve()), profile=profile,
        start=text(start), end=text(end), tail_start=text(end-timedelta(minutes=1)),
        drill_end=text(end+timedelta(minutes=1)), minutes=minutes,
        uni=[f'S{n:04d}' for n in range(554)], focus=[f'S{n:04d}' for n in range(40)],
        hot=[f'S{n:04d}' for n in range(40)], outside='S0554', source='synthetic_only',
        grids={k:list(v) for k,v in GRIDS.items()}, dispatch_deadline_seconds=.75,
        tail_batch_delays={'uni':.18, 'focus':.20, 'hot':.25}, grace_seconds=2., residence_seconds=4.)


def load_config(folder):
    folder = Path(folder).resolve()
    config = read_json(folder/'config.json')
    require(config.get('contract') == CONTRACT and config.get('run_root') == str(folder) and config.get('run_id') == folder.name, 'Run identity/path differs')
    require(read_json(folder/'identity.json') == dict(contract=CONTRACT, config_sha256=hashlib.sha256((folder/'config.json').read_bytes()).hexdigest()), 'Configuration changed')
    expected = configuration(folder, config['profile'])
    # The selected future date is retained across restarts and midnight.
    for key in ('start','end','tail_start','drill_end'):
        expected[key] = config[key]
    require(config == expected, 'Unsupported experiment settings')
    start,end,tail,drill = (datetime.fromisoformat(config[k]) for k in ('start','end','tail_start','drill_end'))
    require(all(v.tzinfo is not None for v in (start,end,tail,drill)) and end-start == timedelta(minutes=config['minutes'])
        and end-tail == drill-end == timedelta(minutes=1), 'Window differs')
    from mb_market_data.watchlist_polling import ET
    require(start.astimezone(ET).strftime('%H:%M:%S.%f') == '09:30:00.000000', 'Synthetic opening differs')
    return config


def store_for(folder, config):
    from mb_market_data.quote_observation_store import QuoteObservationStore
    return QuoteObservationStore(Path(folder)/'quotes.sqlite3', session_date=datetime.fromisoformat(config['start']).date(), schema_version=2)


class RichClient:
    def __init__(self, folder, channel, config, clock, delay=0.):
        self.folder,self.channel,self.config,self.clock,self.delay = Path(folder),channel,config,clock,delay
        self.calls = 0

    def quotes(self, symbols, *, fields):
        from provider_load import SyntheticResponse
        require(fields == 'all' and 0 < len(symbols) <= 400 and set(symbols) <= set(self.config['uni']+[self.config['outside']]), 'Synthetic batch differs')
        append_line(self.folder/(self.channel+'_calls.jsonl'), {'at_utc':text(self.clock()), 'symbols':len(symbols)})
        self.calls += 1
        if self.delay: time.sleep(self.delay)
        millis = int(self.clock().timestamp()*1000)
        payload = {}
        for n,s in enumerate(symbols):
            price = 10+int(s[1:])/100+self.calls/100000
            payload[s] = dict(symbol=s, assetMainType='EQUITY', assetSubType='COE', realtime=False,
                reference=dict(exchange='SYNTHETIC', description='Offline history instrument '+s, cusip='SYNTHETIC'),
                fundamental=dict(sharesOutstanding=10000000+n, avg10DaysVolume=250000+n, avg1YearVolume=350000+n, peRatio=0.),
                regular=dict(regularMarketLastPrice=price, regularMarketLastSize=100, regularMarketTradeTime=millis, regularMarketNetChange=.25),
                extended=dict(lastPrice=price, bidPrice=price-.01, askPrice=price+.01, totalVolume=n, quoteTime=millis, tradeTime=millis),
                quote=dict(lastPrice=price, bidPrice=price-.01, askPrice=price+.01, mark=price, bidSize=10, askSize=20,
                    lastSize=100, totalVolume=1000+n, openPrice=10., highPrice=price+1, lowPrice=9., closePrice=9.75,
                    quoteTime=millis, tradeTime=millis, bidTime=millis, askTime=millis, securityStatus='Synthetic offline quote'))
        return SyntheticResponse(payload)


def load_poller(folder, channel, clock, *, delay=0., initialize=True):
    from mb_market_data.sampling_acquisition import AcquisitionSettings
    from mb_market_data.sampling_incremental import IncrementalSamplingPoller
    from mb_market_data.sampling_membership import SamplingChannel
    from mb_market_data.watchlist_polling import PollWindow
    config = load_config(folder)
    client = RichClient(folder,channel,config,clock,delay)
    settings = AcquisitionSettings(SamplingChannel(channel), GRIDS[channel], PollWindow(
        datetime.fromisoformat(config['start']), datetime.fromisoformat(config['drill_end'])),400,'all',CONTRACT,.75,.05)
    poller = IncrementalSamplingPoller(store_for(folder,config),settings=settings,client=client,clock=clock)
    if initialize: poller.initialize()
    return poller,client


def load_control(folder, clock):
    from mb_watchlist_coordinator.sampling_admission import AdmissionPolicy
    from mb_watchlist_coordinator.sampling_journal import SamplingJournal
    from mb_watchlist_coordinator.sampling_policy import SamplingPolicy
    from mb_watchlist_coordinator.sampling_projection import SamplingMembership
    from mb_watchlist_coordinator.sampling_request_store import SamplingRequestStore
    from mb_watchlist_coordinator.sampling_service import PublicationSettings, SamplingPublicationService
    config=load_config(folder)
    baseline=SamplingMembership(frozenset(config['uni']),frozenset(config['focus']),frozenset(config['hot']))
    admission=AdmissionPolicy(CONTRACT+'-catalog',frozenset(config['uni']+[config['outside']]),frozenset({'manual','synthetic-halt'}),1,'session')
    ledger=SamplingRequestStore(Path(folder)/'requests.sqlite3',session_date=store_for(folder,config).session_date,
        baseline=baseline,admission=admission,policy=SamplingPolicy('review-super-user',2.,4.,'immediate_cap'),clock=clock)
    ledger.initialize()
    service=SamplingPublicationService(Path(folder)/'publication.sqlite3', request_store=ledger,
        journal=SamplingJournal(store_for(folder,config)),settings=PublicationSettings(CONTRACT,.25,.5,datetime.fromisoformat(config['end'])),clock=clock)
    service.initialize()
    return ledger,service


def patched_clock(clock):
    class LogicalDatetime(datetime):
        @classmethod
        def now(cls,tz=None): return clock().astimezone(tz or timezone.utc)
    return patch('mb_market_data.schwab_quotes.datetime',LogicalDatetime)


def slots(config, channel, end=None):
    start=datetime.fromisoformat(config['start']); end=end or datetime.fromisoformat(config['end'])
    at=start
    while at<end:
        for second in GRIDS[channel]:
            value=at.replace(second=second)
            if value<end: yield value
        at+=timedelta(minutes=1)


def compact_status(value):
    return {k:v for k,v in value.items() if k!='symbols'}


def stopped(folder):
    if (Path(folder)/'STOP').exists(): raise InterruptedError('Offline experiment stopped')


def prepare(folder, profile, on_status):
    from mb_market_data.sampling_membership import SamplingHierarchyRevision
    from mb_market_data.sampling_acquisition import ExplicitPollSlot, MARKER
    from mb_market_data.sampling_incremental import INCREMENTAL_MARKER
    from mb_market_data.sampling_membership import SamplingChannel
    folder=Path(folder)
    require(not folder.exists(), 'Fresh output directory required')
    require(not any(v.lower().startswith('quote_observation_journal') for v in folder.parts), 'Production paths excluded')
    require(shutil.disk_usage(folder.parent).free >= (6 if profile=='full' else 1)*1024**3, 'Insufficient free space for the selected offline proof')
    folder.mkdir()
    config=configuration(folder,profile)
    write_json(folder/'config.json',config)
    write_json(folder/'identity.json',dict(contract=CONTRACT,config_sha256=hashlib.sha256((folder/'config.json').read_bytes()).hexdigest()))
    journal=store_for(folder,config); journal.initialize()
    journal.record_membership_revision(SamplingHierarchyRevision(session_date=journal.session_date,revision=0,
        effective_at=datetime.fromisoformat(config['start']),uni_symbols=config['uni'],focus_symbols=config['focus'],hot_symbols=config['hot'],
        source=CONTRACT,metadata={MARKER:True,INCREMENTAL_MARKER:True,'synthetic':True,'history_contract':CONTRACT}))
    clock=Clock(folder,config,fixed=datetime.fromisoformat(config['start']))
    # All schema epochs are established before any warmed collector pins them.
    for channel in CHANNELS: load_poller(folder,channel,clock)
    prefill={}
    for channel in CHANNELS:
        stopped(folder)
        clock.fixed=datetime.fromisoformat(config['start'])
        poller,_=load_poller(folder,channel,clock)
        before=poller.full_reconstructions
        planned=list(slots(config,channel,datetime.fromisoformat(config['tail_start'])))
        began=time.perf_counter(); next_progress=began
        with patched_clock(clock):
            for n,at in enumerate(planned):
                stopped(folder); clock.fixed=at
                outcome=poller.sample(ExplicitPollSlot(SamplingChannel(channel),at,GRIDS[channel]))
                require(outcome['status']=='acquired', 'Logical prefill did not acquire')
                append_line(folder/(channel+'_prefill.jsonl'),compact_status(outcome))
                if time.perf_counter()>=next_progress or n+1==len(planned):
                    on_status(f'Logical prefill {channel.upper()}: {n+1}/{len(planned)} acquisitions; elapsed {time.perf_counter()-began:.1f}s')
                    next_progress=time.perf_counter()+10
        require(poller.full_reconstructions==before,'Unexpected full scan during native prefill')
        prefill[channel]=dict(acquisitions=len(planned),elapsed_seconds=time.perf_counter()-began,no_full_scan=True)
        del poller; gc.collect()
    clock.fixed=datetime.fromisoformat(config['tail_start'])-timedelta(seconds=5)
    load_control(folder,clock)
    write_json(folder/'prefill.json',prefill)
    return config


def worker(folder, mode, *, drill=None):
    from mb_market_data.hot_provider_trial import DeadlineStop
    from mb_market_data.sampling_acquisition import ExplicitPollSlot
    from mb_market_data.sampling_membership import SamplingChannel
    config=load_config(folder)
    clock=Clock(folder,config)
    event=Event()
    def watch():
        while not event.wait(.05):
            if (folder/'STOP').exists(): event.set()
    Thread(target=watch,daemon=True).start()
    def status(v): append_line(folder/(mode+'_tail.jsonl'),dict(compact_status(v),at_utc=text(clock()),pid=os.getpid()))
    try:
        began=time.perf_counter()
        if mode=='coordinator':
            _,service=load_control(folder,clock)
            status(dict(status='ready',cold_start_seconds=time.perf_counter()-began))
            while clock.arm is None and not event.wait(.05): clock()
            if not event.is_set(): status(service.run(event,on_status=status))
        else:
            if drill:
                clock.fixed=datetime.fromisoformat(drill['at'])
            poller,client=load_poller(folder,mode,clock,delay=0. if drill else config['tail_batch_delays'][mode])
            cold=time.perf_counter()-began
            if drill:
                with patched_clock(clock):
                    if drill['operation']=='crash':
                        slot=ExplicitPollSlot(SamplingChannel(mode),clock(),GRIDS[mode])
                        def fault(stage):
                            if stage==drill['stage']: os._exit(73)
                        poller.sample(slot,fault_hook=fault)
                        raise RuntimeError('Requested crash boundary not reached')
                    before=client.calls
                    recovered=poller.recover()
                    repeated=poller.sample(ExplicitPollSlot(SamplingChannel(mode),clock(),GRIDS[mode]))
                    require(client.calls==before and repeated['status']=='already_finished','Recovery repeated a quote request')
                write_json(folder/('drill_'+drill['stage']+'.json'),dict(cold_start_seconds=cold,recovered=recovered,repeated=repeated,quote_calls=client.calls))
            else:
                full=poller.full_reconstructions
                status(dict(status='ready',cold_start_seconds=cold,full_reconstructions=full))
                while clock.arm is None and not event.wait(.05): clock()
                if not event.is_set():
                    stop=DeadlineStop(datetime.fromisoformat(config['end']),event,clock=clock)
                    with patched_clock(clock): result=poller.run_loop(stop,on_status=status)
                    status(dict(result,no_full_scan_during_polling=poller.full_reconstructions==full))
        return 0
    finally: event.set()


def spawn(folder, mode, *, drill=None):
    name=mode if drill is None else 'drill_'+drill['stage']+'_'+drill['operation']
    handle=(folder/(name+'_console.txt')).open('w',encoding='utf-8')
    args=[sys.executable,'-I','-B',str(HERE/'worker.py'),str(folder),mode]
    if drill: args.append(json.dumps(drill))
    try: child=subprocess.Popen(args,stdout=handle,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL)
    finally: handle.close()
    return child


def wait_child(child, folder, *, timeout=900, on_status=print):
    deadline=time.monotonic()+timeout; progress=time.monotonic()+10
    while child.poll() is None:
        stopped(folder)
        require(time.monotonic()<deadline,'Owned worker exceeded startup/recovery timeout')
        if time.monotonic()>=progress:
            on_status('Waiting for owned history/recovery worker; detailed output retained.');progress=time.monotonic()+10
        time.sleep(.1)
    return child.returncode


def run_tail(folder,config,on_status,children):
    clock=Clock(folder,config)
    for mode in MODES:
        on_status('Cold reconstruction: '+mode.upper()+' (serialized startup; scheduling clock held)')
        children[mode]=spawn(folder,mode)
        deadline=time.monotonic()+900;progress=time.monotonic()+10
        while not any(r['status']=='ready' for r in lines(folder/(mode+'_tail.jsonl'))):
            stopped(folder)
            require(all(c.poll() is None for c in children.values()),'Worker exited during cold startup; inspect console files')
            require(time.monotonic()<deadline,'Cold startup exceeded 15 minutes')
            if time.monotonic()>=progress:
                on_status('Still reconstructing '+mode.upper()+' full stored history.');progress=time.monotonic()+10
            time.sleep(.1)
        ready=lines(folder/(mode+'_tail.jsonl'))[0]
        on_status('  Ready; cold start '+str(round(ready['cold_start_seconds'],3))+'s')
    ledger,_=load_control(folder,clock)
    write_json(folder/'ARM.json',dict(run_id=config['run_id'],actual_utc=text(utc_now()),logical_utc=text(clock())))
    on_status('All four processes ready. Actual elapsed tail: 60 seconds, Hot every 2s; clock now advances normally.')
    start=datetime.fromisoformat(config['tail_start']); end=datetime.fromisoformat(config['end'])
    actions=[(5,'manual'),(10,'halt'),(15,'withdraw_manual'),(25,'cap'),(45,'withdraw_halt')]
    done=set(); next_progress=time.monotonic()
    while clock()<end:
        stopped(folder)
        require(all(c.poll() is None or (c.returncode==0 and lines(folder/(m+'_tail.jsonl'))[-1]['status'] in ('stopped','closed')) for m,c in children.items()),'Worker exited during elapsed tail; inspect console files')
        elapsed=(clock()-start).total_seconds()
        for second,action in actions:
            if elapsed<second or action in done:continue
            from mb_watchlist_coordinator.models import ProducerIntent,IntentType
            if action in ('manual','halt'):
                owner='manual' if action=='manual' else 'synthetic-halt'
                outcome=ledger.submit_request(ProducerIntent(action,owner,IntentType.ENSURE_PRESENT,frozenset({config['outside']}),
                    ledger.clock(),expires_at=end,metadata={'session_date':ledger.session_date.isoformat(),'sampling_channel':'hot','synthetic':True}),actor_id=owner)
            elif action=='cap':
                outcome=ledger.restrict_symbol('cap',symbol=config['outside'],max_group='uni',actor_id='review-super-user',expires_at=start+timedelta(seconds=35),reason='Offline history recovery proof')
            else:
                owner,rid=('manual','manual') if action=='withdraw_manual' else ('synthetic-halt','halt')
                outcome=ledger.cancel_request(action,owner_id=owner,request_id=rid,actor_id=owner)
            require(outcome.event.accepted,'Tail input rejected')
            append_line(folder/'actions.jsonl',dict(action=action,at_utc=text(clock())))
            on_status('  Scenario: '+action);done.add(action)
        if time.monotonic()>=next_progress:
            on_status(f'Actual elapsed tail {max(0,elapsed):.0f}/60s; '+str({c:sum(r['status']=='acquired' for r in lines(folder/(c+'_tail.jsonl'))) for c in CHANNELS}))
            next_progress=time.monotonic()+10
        time.sleep(.05)
    require(len(done)==len(actions),'Tail scenario incomplete')
    for child in children.values(): require(wait_child(child,folder,timeout=15,on_status=on_status)==0,'Tail worker failed')


def recovery_drills(folder,config,on_status,children):
    stages=('after_assembled','after_journal_commit','after_dispatch','after_response')
    reports={}
    for n,stage in enumerate(stages):
        stopped(folder)
        at=datetime.fromisoformat(config['end'])+timedelta(seconds=n*2)
        drill=dict(at=text(at),stage=stage,operation='crash')
        on_status('Recovery drill '+stage+': cold restart, deliberate process exit, then another cold restart.')
        child=spawn(folder,'hot',drill=drill);children['drill']=child
        require(wait_child(child,folder,on_status=on_status)==73,'Crash did not reach exact injected boundary')
        drill['operation']='recover'
        child=spawn(folder,'hot',drill=drill);children['drill']=child
        require(wait_child(child,folder,on_status=on_status)==0,'Recovery worker failed')
        result=read_json(folder/('drill_'+stage+'.json'))
        require(result['recovered']['status']==('acquired' if n<2 else 'unresolved_dispatch'),'Unexpected recovery classification')
        reports[stage]=result
        on_status('  '+result['recovered']['status']+'; additional quote requests: '+str(result['quote_calls']))
    return reports


def analyse(folder,config,drills,on_status):
    from mb_market_data.hot_provider_trial import replay_digest,timing,growth_timings
    from mb_market_data.quote_journal_audit import audit_quote_journal
    from mb_market_data.sampling_assessment import assess
    from mb_watchlist_coordinator.sampling_requests import plain
    end=datetime.fromisoformat(config['end']); store=store_for(folder,config)
    on_status('Independent full audit and two acquisition-aware replays; this reads the accumulated history.')
    began=time.perf_counter()
    # Drill slots are outside the regular-session proof. Audit them separately.
    audit=audit_quote_journal(store.database_path,audited_at=end-timedelta(microseconds=1))
    first=replay_digest(store.database_path);on_status('  Full replay 1 complete.')
    second=replay_digest(store.database_path);on_status('  Full replay 2 complete.')
    failures=[]; channels={}
    if not audit.passed:failures.append('Regular-session audit failed: '+str(audit.findings))
    if first!=second:failures.append('Repeated full replays differ')
    for channel in CHANNELS:
        pre=lines(folder/(channel+'_prefill.jsonl')); tail=lines(folder/(channel+'_tail.jsonl'))
        acquired=[r for r in tail if r['status']=='acquired']
        actual=[datetime.fromisoformat(r['slot_id'].split(':',1)[1]) for r in pre+acquired]
        planned=list(slots(config,channel))
        complete=len(actual)==len(planned) and set(actual)==set(planned)
        clean=tail[-1].get('no_full_scan_during_polling') is True
        if not complete or not clean:failures.append(channel+' session coverage or steady-state guard failed')
        channels[channel]=dict(session_acquisitions=len(actual),expected=len(planned),missing=len(set(planned)-set(actual)),
            elapsed_tail_acquisitions=len(acquired),no_full_scan_during_polling=clean,cold_start_seconds=tail[0]['cold_start_seconds'],
            elapsed_timings={key:timing([r[key] for r in acquired if key in r]) for key in ('cycle_seconds','verification_seconds','journal_seconds','dispatch_lateness_seconds')},
            logical_prefill_growth=growth_timings(pre))
    revisions=store.membership_revisions_in_effective_order(); target=config['outside']
    trace=[(target in r.uni_symbols,target in r.focus_symbols,target in r.hot_symbols) for r in revisions]
    compact=[v for n,v in enumerate(trace) if n==0 or v!=trace[n-1]]
    if compact!=[(False,False,False),(True,True,True),(True,False,False),(True,True,True),(True,False,False)]:
        failures.append('Coordinator tail admission/cap/reentry/demotion trace differs: '+str(compact))
    clock=Clock(folder,config,fixed=end)
    ledger,service=load_control(folder,clock); reopened,again=load_control(folder,clock)
    if [s.payload() for s in ledger.states()]!=[s.payload() for s in reopened.states()] or plain(service.snapshot())!=plain(again.snapshot()):
        failures.append('Coordinator restart differs')
    with closing(sqlite3.connect(store.database_path.resolve().as_uri()+'?mode=ro',uri=True)) as c:
        drill_rows=list(c.execute('SELECT scheduled_at_utc FROM quote_acquisition WHERE channel=? AND scheduled_at_utc>=? ORDER BY scheduled_at_utc',('hot',end.strftime('%Y-%m-%dT%H:%M:%S.%fZ'))))
    require(len(drill_rows)==2,'Expected exactly two salvaged drill acquisitions')
    counts={c.channel:dict(c.status_counts) for c in audit.channels}
    assessment=assess(collection_failures=failures,status_counts=counts)
    report=dict(contract=CONTRACT,result=assessment['result'],assessment=assessment,failures=failures,
        profile=config['profile'],minutes=config['minutes'],source='synthetic_only',network_disabled=True,
        clock='Logical native prefill; last minute scheduled at actual elapsed speed with future offset; drills use logical time',
        prefill=read_json(folder/'prefill.json'),channels=channels,table_counts=dict(audit.table_counts),audit_passed=audit.passed,
        replays_identical=first==second,journal_replay=first,recovery_drills=drills,expected_injected_gaps=2,
        target_membership_trace=compact,hierarchy_revisions=len(revisions),audit_replay_seconds=time.perf_counter()-began,
        storage_bytes={p.name:p.stat().st_size for p in folder.glob('*.sqlite3')},
        limitations=['Synthetic payloads and injected latency; no Schwab capacity or Nasdaq latency claim',
            'Only the final minute uses actual elapsed scheduling; prefill and recovery clocks are logical',
            'Fixed 554/40/40 membership during prefill; Coordinator lifecycle is exercised during the final minute',
            'Two deliberate recovery gaps occur after the session; they are not ordinary-session missed slots',
            'Review settings only; no source install, production journal access or production defaults'])
    write_json(folder/'report.json',report)
    summary=['OFFLINE POLLING HISTORY: '+report['result'],str(config['minutes'])+' session minutes of native synthetic history; final minute uses actual elapsed scheduling.']
    for channel,value in channels.items():
        summary.extend([channel.upper()+': session '+str(value['session_acquisitions'])+'/'+str(value['expected'])+'; missing='+str(value['missing'])+'; elapsed tail='+str(value['elapsed_tail_acquisitions']),
            '  Cold start seconds: '+str(round(value['cold_start_seconds'],3))+'; tail cycle ms: '+str(value['elapsed_timings']['cycle_seconds'])])
    summary+=['Recovery: saved response and committed response salvaged without requery; two unknown outcomes retained as explicit gaps.',
        'Audit: '+str(audit.passed)+'; identical full replays: '+str(first==second),'Journal replay SHA-256: '+first['sequence_sha256'],*failures,*report['limitations']]
    (folder/'summary.txt').write_text('\n'.join(summary)+'\n',encoding='utf-8')
    on_status('\n'.join(summary))
    return report


def run(folder,profile,on_status=print):
    children={}
    try:
        config=prepare(folder,profile,on_status)
        run_tail(folder,config,on_status,children)
        drills=recovery_drills(folder,config,on_status,children)
        return analyse(folder,config,drills,on_status)
    finally:
        if Path(folder).is_dir():(Path(folder)/'STOP').write_text('experiment finished or stopped\n',encoding='utf-8')
        for child in children.values():
            if child.poll() is None:
                try:child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill();child.wait(timeout=5)
