"""Rich synthetic prefill followed by actual elapsed scheduling; no network."""
from __future__ import annotations
import json,time
from datetime import datetime,timedelta,timezone
from pathlib import Path
from threading import Event
from unittest.mock import patch
from mb_market_data.hot_provider_trial import SyntheticResponse,replay_digest,timing,write_json
from mb_market_data.sampling_acquisition import AcquisitionSettings,ExplicitPollSlot,MARKER
from mb_market_data.sampling_incremental import IncrementalSamplingPoller,INCREMENTAL_MARKER
from mb_market_data.sampling_membership import SamplingChannel,SamplingHierarchyRevision
from mb_market_data.quote_observation_store import QuoteObservationStore
from mb_market_data.quote_journal_audit import audit_quote_journal
from mb_market_data.watchlist_polling import PollWindow

ROSTER=tuple('NCPL TCRT WFF SNTI GPUS ESLA TWG FNGR BAOS BURU HAIN SOAR EONR WSHP AMOD OMEX ATCH INV MRNO OLB ARAI DDC FFAI BEEM GNS TC JBDI FFR BGL NWGL VNTG BENF SURG MOBX BEAT AZIO XHLD QNME MTEK PMI'.split())

class Clock:
    def __init__(self,value):self.value=value;self.offset=None
    def __call__(self):return self.value if self.offset is None else datetime.now(timezone.utc)+self.offset
    def start_elapsed(self,value):self.offset=value-datetime.now(timezone.utc)

class RichClient:
    def __init__(self):self.count=0;self.delay=0.0
    def quotes(self,symbols,*,fields):
        time.sleep(self.delay);self.count+=1
        response=SyntheticResponse(symbols,self.count)
        for n,(symbol,row) in enumerate(response.payload.items()):
            row.update(assetSubType='COE',symbol=symbol)
            row['reference']={'exchange':'Q','exchangeName':'SYNTHETIC','description':'Synthetic scheduling fixture '+symbol,'cusip':'SYNTHETIC'}
            row['fundamental']={'sharesOutstanding':10000000+n,'avg10DaysVolume':250000+n,'avg1YearVolume':350000+n,'peRatio':0.0,'netProfitMarginMRQ':1.0}
            row['regular']={'regularMarketLastPrice':100.25,'regularMarketLastSize':100,'regularMarketTradeTime':1792418400000,'regularMarketNetChange':.25,'regularMarketPercentChange':.25}
            row['extended']={'bidPrice':100.24,'askPrice':100.26,'mark':100.25,'lastPrice':100.25,'lastSize':100,'totalVolume':n,'quoteTime':1792418400000,'tradeTime':1792418400000}
            row['quote'].update(bidPrice=100.24,askPrice=100.26,mark=100.25,bidSize=10,askSize=20,lastSize=100,openPrice=100.0,highPrice=101.0,lowPrice=99.0,closePrice=99.75,netChange=.5,netPercentChange=.5,quoteTime=1792418400000,tradeTime=1792418400000,bidTime=1792418400000,askTime=1792418400000,postMarketChange=0.0,postMarketPercentChange=0.0)
        return response

def run(output,*,prefill=300,elapsed_slots=8,on_status=print):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    base=(datetime.now(timezone.utc)+timedelta(days=1)).replace(hour=14,minute=0,second=0,microsecond=0)
    clock=Clock(base);client=RichClient();end=base+timedelta(seconds=2*(prefill+elapsed_slots))
    journal=QuoteObservationStore(output/'quotes.sqlite3',session_date=base.date(),schema_version=2);journal.initialize()
    journal.record_membership_revision(SamplingHierarchyRevision(session_date=base.date(),revision=0,effective_at=base,uni_symbols=ROSTER,focus_symbols=ROSTER,hot_symbols=ROSTER,source='rich_synthetic_elapsed_proof',metadata={MARKER:True,INCREMENTAL_MARKER:True,'synthetic':True,'clock':'future offset; elapsed scheduling after logical prefill'}))
    settings=AcquisitionSettings(SamplingChannel.HOT,tuple(range(0,60,2)),PollWindow(base,end),400,'all','rich_synthetic_elapsed_proof',.75,.05)
    poller=IncrementalSamplingPoller(journal,settings=settings,client=client,clock=clock);poller.initialize()
    full_before=poller.full_reconstructions
    class ClockDatetime(datetime):
        @classmethod
        def now(cls,tz=None):return clock().astimezone(tz or timezone.utc)
    with patch('mb_market_data.schwab_quotes.datetime',ClockDatetime):
        for n in range(prefill):
            clock.value=base+timedelta(seconds=n*2)
            value=poller.sample(ExplicitPollSlot(SamplingChannel.HOT,clock(),settings.seconds))
            if value['status']!='acquired':raise RuntimeError('Prefill did not acquire')
            if (n+1)%50==0:on_status('Logical prefill: '+str(n+1)+'/'+str(prefill)+' acquisitions; '+str((n+1)*len(ROSTER))+' observations')
        # Startup/recovery scans have finished before this timed loop. Actual elapsed
        # work and waits advance the clock, so slow validation can miss a deadline.
        client.delay=.25
        clock.start_elapsed(base+timedelta(seconds=prefill*2-1.8))
        outcomes=[];started=time.perf_counter()
        on_status('Actual elapsed scheduling: '+str(elapsed_slots)+' slots at 2 seconds; injected synthetic request latency 250 ms; 750 ms dispatch deadline unchanged.')
        def status(value):
            outcomes.append(value);on_status('  Elapsed slot: '+value['status'])
        try:
            loop=poller.run_loop(Event(),on_status=status)
        except Exception as exc:
            write_json(output/'failure.json',{'passed':False,'exception_type':type(exc).__name__,'detail':str(exc),'synthetic':True,'elapsed_seconds':time.perf_counter()-started,'outcomes':outcomes,'journal':str(journal.database_path),'sidecar':str(poller.path)})
            raise
        elapsed=time.perf_counter()-started
    no_scan=poller.full_reconstructions==full_before
    state=poller.snapshot();audit=audit_quote_journal(journal.database_path,audited_at=end)
    first,second=replay_digest(journal.database_path),replay_digest(journal.database_path)
    acquired=sum(v['status']=='acquired' for v in outcomes)
    metrics={key:timing([v[key] for v in outcomes if key in v]) for key in ('cycle_seconds','verification_seconds','state_prepare_seconds','spool_and_check_seconds','before_dispatch_gate_seconds','before_api_gate_seconds','recovery_before_slot_seconds')}
    passed=acquired==elapsed_slots and no_scan and audit.passed and first==second and state['pending'] is None
    report={'passed':passed,'synthetic':True,'prefill_clock':'logical','timing_clock':'actual elapsed with future offset; not market timestamps','prefill_acquisitions':prefill,'prefill_observations':prefill*len(ROSTER),'elapsed_planned_slots':elapsed_slots,'elapsed_acquired':acquired,'elapsed_seconds':elapsed,'loop_result':loop,'full_reconstructions_before':full_before,'no_full_reconstruction_during_polling':no_scan,'full_reconstructions_after_audit':poller.full_reconstructions,'dispatch_deadline_seconds':.75,'injected_request_latency_seconds':.25,'timings':metrics,'outcomes':outcomes,'audit_passed':audit.passed,'replays_identical':first==second,'replay':first,'limitations':['Rich synthetic decoded payloads; captured Schwab payload replay pending','Local scheduling evidence; no brokerage or market capacity claim']}
    write_json(output/'report.json',report)
    lines=['Guarded incremental elapsed proof: '+('PASS' if passed else 'FAILED'),'Prefill: '+str(prefill)+' acquisitions / '+str(prefill*len(ROSTER))+' rich synthetic rows','Actual elapsed slots: '+str(acquired)+'/'+str(elapsed_slots)+'; seconds='+str(round(elapsed,3)),'Full history scan during polling: '+str(not no_scan),'Audit: '+str(audit.passed)+'; repeated replay identical: '+str(first==second)]
    lines += [key+': '+str(value) for key,value in metrics.items()]+report['limitations']
    (output/'summary.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8');on_status('\n'.join(lines))
    return report
