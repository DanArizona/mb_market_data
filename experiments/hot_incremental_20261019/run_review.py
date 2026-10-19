"""Pinned source check, focused tests, and elapsed scheduling proof."""
from __future__ import annotations
import importlib.util,sys,unittest,uuid
from datetime import datetime,timezone
from pathlib import Path
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
sys.path.insert(0,str(ROOT/'experiments/hot_provider_trial_20261018'))
from bundle_guard import verify

class Tee:
    def __init__(self,*streams):self.streams=streams
    def write(self,value):
        for stream in self.streams:stream.write(value);stream.flush()
    def flush(self):
        for stream in self.streams:stream.flush()

def main():
    try:manifest=verify(ROOT,'experiments/hot_incremental_20261019/source_manifest.json')
    except Exception as exc:print('Source verification FAILED: '+str(exc),flush=True);return 1
    sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(HERE))
    from mb_market_data.sampling_incremental import IncrementalSamplingPoller
    import mb_market_data.sampling_incremental as source
    if Path(source.__file__).resolve()!=ROOT/'src/mb_market_data/sampling_incremental.py':raise ValueError('Unexpected source')
    print('GUARDED INCREMENTAL HOT - offline verification; no API/password',flush=True)
    print('Source: '+str(source.__file__),flush=True)
    output=ROOT/'runs'/('incremental_verify_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:6]);output.mkdir(parents=True,exist_ok=False)
    loader=unittest.TestLoader()
    suite=unittest.TestSuite(loader.discover(str(ROOT/'tests'),pattern=pattern) for pattern in ('test_sampling_acquisition.py','test_sampling_incremental.py','test_hot_provider_trial.py','test_hot_incremental_review.py','test_hot_scale.py','test_hot_trial_bundle.py'))
    with (output/'tests.txt').open('w',encoding='utf-8') as file:
        passed=unittest.TextTestRunner(stream=Tee(sys.stdout,file),verbosity=2).run(suite).wasSuccessful()
    if not passed:print('Offline verification FAILED: tests',flush=True);return 1
    from elapsed_proof import run
    try:
        report=run(output/'elapsed',on_status=lambda s:print(s,flush=True))
    except Exception as exc:
        print('Elapsed verification FAILED: '+str(exc),flush=True)
        print('Preserved reports and databases: '+str(output),flush=True)
        return 1
    print('Reports: '+str(output),flush=True)
    print('Next: run 06_export_hot_trial_payloads.cmd and attach its ZIP for real-payload replay. No new Schwab trial is launched by this bundle.',flush=True)
    print('Finished. This command returns to its prompt.',flush=True)
    return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
