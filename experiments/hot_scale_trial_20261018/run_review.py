"""Bounded larger-roster trial and isolated offline growth proof."""
from __future__ import annotations
import importlib.util
import sys
import unittest
import uuid
from datetime import datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import patch

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
sys.path.insert(0,str(ROOT/'experiments/hot_provider_trial_20261018'))
from bundle_guard import verify
legacy_spec=importlib.util.spec_from_file_location('hot_provider_legacy_launcher',ROOT/'experiments/hot_provider_trial_20261018/run_review.py')
legacy=importlib.util.module_from_spec(legacy_spec);legacy_spec.loader.exec_module(legacy)
Tee=legacy.Tee

# October 16 validated opening Focus roster; fixed test input, no production publication.
ROSTER=tuple('NCPL TCRT WFF SNTI GPUS ESLA TWG FNGR BAOS BURU HAIN SOAR EONR WSHP AMOD OMEX ATCH INV MRNO OLB ARAI DDC FFAI BEEM GNS TC JBDI FFR BGL NWGL VNTG BENF SURG MOBX BEAT AZIO XHLD QNME MTEK PMI'.split())


class LogicalClock:
    def __init__(self):
        self.value=(datetime.now(timezone.utc)+timedelta(days=1)).replace(hour=14,minute=0,second=0,microsecond=0)
    def __call__(self):return self.value
    def wait(self,seconds):self.value+=timedelta(seconds=seconds)


def main():
    if len(sys.argv)!=2 or sys.argv[1] not in ('verify','schwab'):
        print('Use the fixed scale verification or Schwab trial CMD launcher.',flush=True);return 2
    try:manifest=verify(ROOT,'experiments/hot_scale_trial_20261018/source_manifest.json')
    except Exception as exc:print('Source verification FAILED: '+str(exc),flush=True);return 1
    sys.path.insert(0,str(ROOT/'src'))
    import mb_market_data.sampling_acquisition as acquisition
    if Path(acquisition.__file__).resolve()!=(ROOT/'src/mb_market_data/sampling_acquisition.py').resolve():
        print('Unexpected acquisition source',flush=True);return 1
    print('Verified source: '+str(acquisition.__file__),flush=True)
    print('Production baseline: '+manifest['market_data_baseline_commit'],flush=True)
    print('Fixed test roster: '+str(len(ROSTER))+' symbols from the validated October 16 Focus roster',flush=True)
    if sys.argv[1]=='schwab':
        print('SCHWAB HOT SCALE TRIAL: 5 minutes at 2s, then 5 minutes at 4s.',flush=True)
        print('225 quote calls maximum; one batch per call; 9000 requested observations; one password prompt.',flush=True)
        spec=importlib.util.spec_from_file_location('scale_probe',ROOT/'probes/probe_hot_quote_trial.py')
        probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
        return probe.main(['--schwab','--symbols',*ROSTER,'--intervals','2','4','--phase-seconds','300',
            '--quote-call-budget','225','--request-timeout-seconds','3','--output-root',str(ROOT/'runs/hot_scale_schwab'),
            '--ecfg',r'C:\Users\danla\MBV\secure_schwabdev.ecfg'])
    print('OFFLINE SCALE CHECK: 60 tests, short actual-clock synthetic run, then logical-time growth proof.',flush=True)
    print('No API/password. Growth proof measures local work; it does not wait ten minutes or simulate provider capacity.',flush=True)
    output=ROOT/'runs'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-scale-verify-'+uuid.uuid4().hex[:6])
    output.mkdir(parents=True,exist_ok=False)
    loader=unittest.TestLoader()
    suite=unittest.TestSuite(loader.discover(str(ROOT/'tests'),pattern=pattern) for pattern in
        ('test_sampling_acquisition.py','test_hot_provider_trial.py','test_hot_trial_bundle.py','test_hot_scale.py'))
    with (output/'tests.txt').open('w',encoding='utf-8') as file:
        passed=unittest.TextTestRunner(stream=Tee(sys.stdout,file),verbosity=2).run(suite).wasSuccessful()
    if not passed:print('Scale verification FAILED: tests',flush=True);return 1
    from mb_market_data.hot_provider_trial import TrialSettings,SyntheticClient,run_trial,write_json
    short=run_trial(output/'actual_clock',TrialSettings(ROSTER,(2,4),4,3,3,'synthetic'),SyntheticClient(),
        on_status=lambda s:print(s,flush=True))
    if not short['passed']:print('Actual-clock synthetic check FAILED',flush=True);return 1
    clock=LogicalClock()
    class LogicalDatetime(datetime):
        @classmethod
        def now(cls,tz=None):return clock().astimezone(tz or timezone.utc)
    count=[0]
    def progress(message):
        if message.endswith(': acquired'):
            count[0]+=1
            if count[0]%10==0:print('Logical growth proof: '+str(count[0])+'/225 acquisitions completed',flush=True)
        elif not message.startswith('  '):print(message,flush=True)
    print('Starting 40-symbol logical growth proof: 150 acquisitions, then 75; progress every 10.',flush=True)
    with patch('mb_market_data.schwab_quotes.datetime',LogicalDatetime):
        report=run_trial(output/'logical_growth',TrialSettings(ROSTER,(2,4),300,225,3,'synthetic'),SyntheticClient(),
            clock=clock,wait=clock.wait,clock_mode='logical_offline',on_status=progress)
    print((output/'logical_growth/summary.txt').read_text(encoding='utf-8'),flush=True)
    print('Offline Hot scale verification: '+('PASS' if report['passed'] else 'FAILED'),flush=True)
    print('Finished. This command returns to its prompt.',flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
