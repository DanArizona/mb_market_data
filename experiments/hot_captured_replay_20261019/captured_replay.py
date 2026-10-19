"""Offline decoded Schwab payload replay, with separate simulation receipt times."""
from __future__ import annotations
import copy,hashlib,json,re,sys,time
from datetime import datetime,timedelta,timezone
from pathlib import Path
from threading import Event
from unittest.mock import patch
from mb_market_data.hot_provider_trial import replay_digest,timing,write_json
from mb_market_data.sampling_acquisition import AcquisitionSettings,ExplicitPollSlot,MARKER
from mb_market_data.sampling_incremental import IncrementalSamplingPoller,INCREMENTAL_MARKER
from mb_market_data.sampling_membership import SamplingChannel,SamplingHierarchyRevision
from mb_market_data.quote_observation_store import QuoteObservationStore
from mb_market_data.quote_journal_audit import audit_quote_journal
from mb_market_data.watchlist_polling import PollWindow

HERE=Path(__file__).resolve().parent
FIXTURE=HERE/'fixtures/quote_payload_samples.json'
def load_config(path=HERE/'review_config.json'):
    config=json.loads(Path(path).read_text())
    if config.get('contract')!='private-captured-review-v1':raise ValueError('Private review configuration required')
    for key in ('fixture_sha256','archive_sha256'):
        if re.fullmatch('[0-9a-f]{64}',config.get(key,'')) is None:raise ValueError('Expected pinned private fixture hashes')
    if not isinstance(config.get('ecfg_path'),str):raise ValueError('Explicit encrypted configuration path required')
    return config

def aware(value):
    parsed=datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:raise ValueError('Source timestamps must be aware')
    return parsed

def load_fixture(path=FIXTURE,*,expected_sha256):
    raw=Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected_sha256:raise ValueError('Captured fixture hash differs')
    def reject_constant(value):raise ValueError('Nonfinite captured value')
    value=json.loads(raw,parse_constant=reject_constant)
    if value.get('contract')!='successful-quote-payload-samples-v1':raise ValueError('Captured fixture contract differs')
    phases=value.get('phases',[])
    if [p.get('name') for p in phases]!=['phase_01_2s','phase_02_4s']:raise ValueError('Expected both original phases')
    samples=[];roster=None
    for phase in phases:
        if len(phase['samples'])!=3:raise ValueError('Expected first/middle/last response samples')
        previous=0
        for sample in phase['samples']:
            if type(sample['source_sequence']) is not int or sample['source_sequence']<=previous:raise ValueError('Source sample sequence differs')
            previous=sample['source_sequence']
            if re.fullmatch('[0-9a-f]{64}',sample['source_event_sha256']) is None:raise ValueError('Source event hash differs')
            completed,recorded=aware(sample['source_completed_at_utc']),aware(sample['source_recorded_at_utc'])
            if completed>recorded:raise ValueError('Source completion follows its receipt')
            quotes=sample['quotes'];symbols=tuple(row['symbol'] for row in quotes)
            if len(symbols)!=40 or len(set(symbols))!=40:raise ValueError('Expected complete forty-symbol response')
            if roster is None:roster=symbols
            if symbols!=roster:raise ValueError('Captured sample roster differs')
            for row in quotes:
                if row['status']!='quote' or not isinstance(row['quote'],dict) or row['quote'].get('symbol')!=row['symbol']:raise ValueError('Expected successful matching decoded quote')
                if not aware(row['request_started_at_utc'])<=aware(row['response_received_at_utc'])<=completed:raise ValueError('Source request/response clock differs')
            samples.append({'phase':phase['name'],**copy.deepcopy(sample)})
    return {'source':copy.deepcopy(value),'samples':samples,'roster':roster,'fixture_sha256':expected_sha256}

class PayloadResponse:
    status_code,ok=200,True
    def __init__(self,payload):self.payload=payload
    def json(self):return self.payload

class CapturedClient:
    """Mock transport reuses exact decoded quotes; it does not replay network timing."""
    def __init__(self,fixture):
        self.fixture=copy.deepcopy(fixture);self.count=0;self.delay=0.0;self.used_samples=[]
    def quotes(self,symbols,*,fields):
        if fields!='all' or tuple(symbols)!=self.fixture['roster']:raise ValueError('Replay request differs from captured roster')
        time.sleep(self.delay)
        index=self.count%len(self.fixture['samples']);sample=self.fixture['samples'][index]
        self.count+=1;self.used_samples.append(index)
        return PayloadResponse({row['symbol']:copy.deepcopy(row['quote']) for row in sample['quotes']})

class Clock:
    def __init__(self,value):self.value=value;self.offset=None
    def __call__(self):return self.value if self.offset is None else datetime.now(timezone.utc)+self.offset
    def start_elapsed(self,value):self.offset=value-datetime.now(timezone.utc)

def run(output,*,fixture_path=FIXTURE,fixture_sha256=None,archive_sha256=None,prefill=300,elapsed_slots=30,on_status=print):
    if fixture_sha256 is None:
        config=load_config();fixture_sha256=config['fixture_sha256'];archive_sha256=config['archive_sha256']
    fixture=load_fixture(fixture_path,expected_sha256=fixture_sha256);output=Path(output)
    if output.exists():raise ValueError('Replay output must be fresh')
    if prefill<1 or elapsed_slots<1:raise ValueError('Positive prefill and elapsed slots required')
    output.mkdir(parents=True,exist_ok=False)
    base=(datetime.now(timezone.utc)+timedelta(days=1)).replace(hour=14,minute=0,second=0,microsecond=0)
    clock=Clock(base);client=CapturedClient(fixture);end=base+timedelta(seconds=2*(prefill+elapsed_slots));roster=fixture['roster']
    journal=QuoteObservationStore(output/'quotes.sqlite3',session_date=base.date(),schema_version=2);journal.initialize()
    provenance={'fixture_sha256':fixture_sha256,'original_archive_sha256':archive_sha256,'original_source_trial':fixture['source']['source_trial'],'source_event_sha256':[s['source_event_sha256'] for s in fixture['samples']],'simulation_clock':'logical prefill then actual elapsed with future offset','original_quote_fields_and_timestamps_unchanged':True}
    write_json(output/'provenance.json',provenance)
    journal.record_membership_revision(SamplingHierarchyRevision(session_date=base.date(),revision=0,effective_at=base,uni_symbols=roster,focus_symbols=roster,hot_symbols=roster,source='captured_decoded_payload_simulation',metadata={MARKER:True,INCREMENTAL_MARKER:True,'offline_simulation':True,**provenance}))
    settings=AcquisitionSettings(SamplingChannel.HOT,tuple(range(0,60,2)),PollWindow(base,end),400,'all','captured_decoded_payload_simulation',.75,.05)
    poller=IncrementalSamplingPoller(journal,settings=settings,client=client,clock=clock);poller.initialize();full_before=poller.full_reconstructions
    class ClockDatetime(datetime):
        @classmethod
        def now(cls,tz=None):return clock().astimezone(tz or timezone.utc)
    outcomes=[];elapsed_started=None;prefill_outcomes=[]
    try:
        with patch('mb_market_data.schwab_quotes.datetime',ClockDatetime):
            for n in range(prefill):
                clock.value=base+timedelta(seconds=n*2)
                value=poller.sample(ExplicitPollSlot(SamplingChannel.HOT,clock(),settings.seconds));prefill_outcomes.append(value)
                if value['status']!='acquired':raise RuntimeError('Captured prefill did not acquire')
                if (n+1)%50==0:on_status('Captured prefill: '+str(n+1)+'/'+str(prefill)+'; observations='+str((n+1)*len(roster)))
            client.delay=.25
            clock.start_elapsed(base+timedelta(seconds=prefill*2-1.8));elapsed_started=time.perf_counter()
            on_status('Captured payload scheduling: '+str(elapsed_slots)+' actual elapsed slots at 2s; injected delay 250ms; deadline 750ms.')
            def status(value):outcomes.append(value);on_status('  Elapsed slot '+str(len(outcomes))+': '+value['status'])
            loop=poller.run_loop(Event(),on_status=status);elapsed=time.perf_counter()-elapsed_started
        no_scan=poller.full_reconstructions==full_before
        state=poller.snapshot();audit=audit_quote_journal(journal.database_path,audited_at=end)
        first,second=replay_digest(journal.database_path),replay_digest(journal.database_path)
    except Exception as exc:
        write_json(output/'failure.json',{'passed':False,'type':type(exc).__name__,'detail':str(exc),'prefill_completed':len(prefill_outcomes),'elapsed_outcomes':outcomes,'journal':str(journal.database_path),'sidecar':str(poller.path)})
        raise
    acquired=sum(v['status']=='acquired' for v in outcomes)
    metrics={key:timing([v[key] for v in outcomes if key in v]) for key in ('cycle_seconds','verification_seconds','state_prepare_seconds','spool_and_check_seconds','before_dispatch_gate_seconds','before_api_gate_seconds','recovery_before_slot_seconds')}
    expected_acquisitions=prefill+elapsed_slots
    assembled=[e for e in state['events'] if e['kind']=='assembled']
    exact_quotes=len(assembled)==client.count and all([r['quote'] for r in event['result']['results']]==[r['quote'] for r in fixture['samples'][i%6]['quotes']] for i,event in enumerate(assembled))
    passed=acquired==elapsed_slots and no_scan and audit.passed and first==second and first['acquisitions']==expected_acquisitions and first['observations']==expected_acquisitions*40 and state['pending'] is None and exact_quotes
    report={'passed':passed,'platform':sys.platform,'source':'six captured decoded Schwab quote responses, reused in offline simulation','fixture_sha256':fixture_sha256,'prefill_clock':'logical','timing_clock':'actual elapsed with future offset; new simulation receipts, not original arrivals','prefill_acquisitions':prefill,'prefill_observations':prefill*40,'elapsed_planned_slots':elapsed_slots,'elapsed_acquired':acquired,'elapsed_seconds':elapsed,'loop_result':loop,'exact_original_quote_payloads':exact_quotes,'samples_used':sorted(set(client.used_samples)),'dispatch_deadline_seconds':.75,'injected_delay_seconds':.25,'no_full_reconstruction_during_polling':no_scan,'full_reconstructions_after_audit':poller.full_reconstructions,'timings':metrics,'prefill_cycle_growth':{label:timing([o['cycle_seconds'] for o in group]) for label,group in [('first_50',prefill_outcomes[:50]),('last_50',prefill_outcomes[-50:])]},'audit_passed':audit.passed,'replays_identical':first==second,'replay':first,'outcomes':outcomes,'limitations':['Captured successful decoded payloads only; source full event chains are not in this export','Synthetic transport delay; no original network replay or live/provider capacity claim','Native synthetic run passed; prior container inode/head anomalies remain unexplained and guards remain strict']}
    write_json(output/'report.json',report)
    lines=['Captured-payload replay: '+('PASS' if passed else 'FAILED'),'Original source: 6 responses / 240 quotes; all quote values and embedded provider times retained','Prefill: '+str(prefill)+' acquisitions / '+str(prefill*40)+' observations','Actual elapsed scheduling: '+str(acquired)+'/'+str(elapsed_slots)+' slots; '+str(round(elapsed,3))+' seconds','Exact decoded payloads: '+str(exact_quotes)+'; full scan during polling: '+str(not no_scan),'Audit: '+str(audit.passed)+'; repeated replay identical: '+str(first==second)]
    lines += [key+': '+str(value) for key,value in metrics.items()]+report['limitations']
    (output/'summary.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8');on_status('\n'.join(lines));return report


def write_gate(root,report_path,*,manifest_sha256,fixture_sha256):
    root=Path(root).resolve();report_path=Path(report_path).resolve()
    if not report_path.is_relative_to(root/'runs'):raise ValueError('Verification report must be inside bundle runs')
    raw=report_path.read_bytes();report=json.loads(raw)
    if not report.get('passed') or report.get('fixture_sha256')!=fixture_sha256 or report.get('prefill_acquisitions',0)<300 or report.get('elapsed_acquired',0)<30 or not report.get('exact_original_quote_payloads'):raise ValueError('Complete captured verification required')
    gate={'platform':report['platform'],'fixture_sha256':fixture_sha256,'source_manifest_sha256':manifest_sha256,'report':report_path.relative_to(root).as_posix(),'report_sha256':hashlib.sha256(raw).hexdigest()}
    write_json(root/'runs/latest_captured_verification.json',gate)


def check_gate(root,*,manifest_sha256,fixture_sha256,platform=None):
    root=Path(root).resolve();gate=json.loads((root/'runs/latest_captured_verification.json').read_text())
    if (platform or sys.platform)!='win32' or gate.get('platform')!='win32':raise ValueError('Native Windows offline verification is required first')
    if gate.get('fixture_sha256')!=fixture_sha256 or gate.get('source_manifest_sha256')!=manifest_sha256:raise ValueError('Offline gate source/fixture differs')
    path=(root/gate['report']).resolve()
    if not path.is_relative_to(root/'runs') or hashlib.sha256(path.read_bytes()).hexdigest()!=gate['report_sha256']:raise ValueError('Offline gate report differs')
    report=json.loads(path.read_text())
    if not report.get('passed') or report.get('elapsed_acquired',0)<30 or report.get('prefill_acquisitions',0)<300 or not report.get('exact_original_quote_payloads') or not report.get('audit_passed') or not report.get('replays_identical') or not report.get('no_full_reconstruction_during_polling'):raise ValueError('Offline gate report incomplete')
    return gate
