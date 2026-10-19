"""Pinned captured-payload verification and gated bounded authenticated successor."""
from __future__ import annotations
import hashlib,importlib.util,sys,unittest,uuid
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
    if len(sys.argv)!=2 or sys.argv[1] not in ('verify','schwab'):
        print('Use 07_verify_captured_hot.cmd or 08_run_schwab_incremental_hot.cmd.',flush=True);return 2
    manifest_path=ROOT/'experiments/hot_captured_replay_20261019/source_manifest.json'
    try:verify(ROOT,str(manifest_path.relative_to(ROOT)))
    except Exception as exc:print('Source verification FAILED: '+str(exc),flush=True);return 1
    manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(HERE))
    import mb_market_data.sampling_incremental as source
    if Path(source.__file__).resolve()!=ROOT/'src/mb_market_data/sampling_incremental.py':raise ValueError('Unexpected incremental source')
    from captured_replay import run,load_fixture,write_gate,check_gate,load_config
    config=load_config()
    print('Verified incremental source: '+str(source.__file__),flush=True)
    if sys.argv[1]=='schwab':
        try:check_gate(ROOT,manifest_sha256=manifest_sha256,fixture_sha256=config['fixture_sha256'])
        except Exception as exc:
            print('Run 07_verify_captured_hot.cmd to PASS on this Windows bundle first. Gate: '+str(exc),flush=True);return 1
        roster=load_fixture(expected_sha256=config['fixture_sha256'])['roster']
        print('BOUNDED SCHWAB SUCCESSOR: 40 symbols; 5 minutes at 2s, then 5 minutes at 4s.',flush=True)
        print('225 planned/maximum quote-client calls; request timeout 3s; dispatch deadline 750ms unchanged.',flush=True)
        print('One password prompt. Fresh isolated journals; production membership and pollers are independent.',flush=True)
        spec=importlib.util.spec_from_file_location('captured_successor_probe',ROOT/'probes/probe_hot_quote_trial.py')
        probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
        return probe.main(['--schwab','--symbols',*roster,'--intervals','2','4','--phase-seconds','300','--quote-call-budget','225','--request-timeout-seconds','3','--verification-mode','guarded_incremental','--output-root',str(ROOT/'runs/hot_incremental_schwab'),'--ecfg',config['ecfg_path']])
    (ROOT/'runs/latest_captured_verification.json').unlink(missing_ok=True)
    print('CAPTURED HOT PAYLOADS - offline, no API/password.',flush=True)
    print('Frozen original samples: first/middle/last successful response from each previous phase.',flush=True)
    output=ROOT/'runs'/('captured_verify_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:6]);output.mkdir(parents=True,exist_ok=False)
    loader=unittest.TestLoader()
    suite=unittest.TestSuite(loader.discover(str(ROOT/'tests'),pattern=pattern) for pattern in ('test_sampling_acquisition.py','test_sampling_incremental.py','test_hot_provider_trial.py','test_hot_incremental_review.py','test_hot_scale.py','test_hot_trial_bundle.py','test_hot_captured_replay.py'))
    with (output/'tests.txt').open('w',encoding='utf-8') as file:
        passed=unittest.TextTestRunner(stream=Tee(sys.stdout,file),verbosity=2).run(suite).wasSuccessful()
    if not passed:print('Captured verification FAILED: tests',flush=True);return 1
    try:
        report=run(output/'elapsed',fixture_sha256=config['fixture_sha256'],archive_sha256=config['archive_sha256'],on_status=lambda s:print(s,flush=True))
        if report['passed']:write_gate(ROOT,output/'elapsed/report.json',manifest_sha256=manifest_sha256,fixture_sha256=config['fixture_sha256'])
    except Exception as exc:
        print('Captured verification FAILED: '+str(exc),flush=True)
        print('Preserved reports and databases: '+str(output),flush=True);return 1
    print('Reports: '+str(output),flush=True)
    if report['passed']:print('Offline captured proof passed. A native Windows PASS allows separately running 08_run_schwab_incremental_hot.cmd.',flush=True)
    print('Finished. This command returns to its prompt.',flush=True)
    return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
