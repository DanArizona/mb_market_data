"""Offline candidate verification. No real SDK calls, vault reads, or live journals."""
from pathlib import Path
import argparse,builtins,getpass,hashlib,importlib.util,json,os,socket,sys,unittest
from datetime import datetime,timezone
from contextlib import redirect_stdout,redirect_stderr
ROOT=Path(__file__).resolve().parent
NATIVE=ROOT/'market_data'
TESTS=['test_daily_membership','test_daily_live_connection','test_daily_three','test_daily_authorization_reporting','test_daily_worker_uptime','test_daily_flow','test_daily_runtime',
       'test_volume_proxy_experiment','test_universe_quote_watch_credentials',
       'test_daily_universe_snapshot','test_daily_universe','test_opening_hierarchy',
       'test_daily_universe_workflow','test_api_overnight_volume','test_schwab_quotes','test_polling_cadence']
EXPECTED_TESTS=304
class Tee:
    def __init__(self,*streams):self.streams=streams
    def write(self,value):
        for s in self.streams:s.write(value);s.flush()
        return len(value)
    def flush(self):
        for s in self.streams:s.flush()
def denied(*a,**k):raise AssertionError('No network or human credentials in offline verification')
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--allow-test-auth-stub',action='store_true',help='Local non-Windows development only')
    args=p.parse_args()
    sys.path[:0]=[str(NATIVE/'src'),str(NATIVE/'tests')]
    os.environ['PYTHONPATH']=os.pathsep.join([str(NATIVE/'src'),str(NATIVE/'tests')])
    output=ROOT/'output'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output.mkdir(parents=True,exist_ok=False)
    os.environ['MB_MEMBERSHIP_PROOF']=str(output/'membership-proof.json')
    try:
        inventory=json.loads((ROOT/'SOURCE_INVENTORY.json').read_text())
        for name,sha in inventory['files'].items():
            path=ROOT/name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=sha:
                raise ValueError('Candidate file differs: '+name)
        import exchange_calendars as xcals
        stub_used=False
        try:import mb_tools.schwab_secure as secure
        except ImportError:
            if not args.allow_test_auth_stub or os.name=='nt':raise
            from daily_flow_auth_stub import install
            install();stub_used=True
        socket.socket.connect=socket.socket.connect_ex=socket.create_connection=socket.getaddrinfo=denied
        getpass.getpass=builtins.input=denied
        print('OFFLINE ONLY: synthetic responses, temporary journals, owned test children.')
        print('Calendar version:',xcals.__version__,'| test-only auth import stub:',stub_used)
        with (output/'console.txt').open('x',encoding='utf-8') as log:
            with redirect_stdout(Tee(sys.stdout,log)),redirect_stderr(Tee(sys.stderr,log)):
                suite=unittest.defaultTestLoader.loadTestsFromNames(TESTS)
                result=unittest.TextTestRunner(verbosity=2).run(suite)
        passed=result.wasSuccessful() and result.testsRun==EXPECTED_TESTS and not result.skipped
        report={'result':'PASS' if passed else 'FAIL','tests_run':result.testsRun,
                'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),
                'platform':sys.platform,'python':sys.version,'exchange_calendars':xcals.__version__,
                'test_only_auth_import_stub':stub_used,
                'actual_child_probe_process_test':True,'controller_clock':'synthetic',
                'membership_tests':47,'live_connection_tests':11,'membership_proof':str(output/'membership-proof.json'),
                'live_nasdaq_verified':False,'live_membership_verified':False,'overlay_render_suite':'separate verify_overlay.cmd',
                'three_channel_tests':41, 'authorization_reporting_tests':28, 'uptime_targeted_tests':40, 'baseline_regressions':137, 'intentional_contract_updates':['interleaved Hot is supported; legacy Hot remains refused'], 'desktop_visual_verified':False, 'three_actual_child_collectors':True, 'actual_monitor_watchdog_process_test':True, 'real_oauth_renewal_verified':False,
                'heartbeat_freshness_seconds':30, 'heartbeat_retry_attempts':4,
                'original_live_shutdown_cause_proven':False,
                'live_uptime_qualified':False,
                'real_concurrent_heartbeat_io_test':True,
                'real_child_heartbeat_fault_injection':True,
                'real_provider_calls':False,'production_changes':False,
                'source_inventory_verified':True,'windows_acceptance':sys.platform=='win32' and passed}
    except Exception as err:
        report={'result':'BLOCKED','error_type':type(err).__name__,'detail':str(err),
                'production_changes':False,'real_provider_calls':False}
    (output/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2));print('Evidence:',output)
    return 0 if report['result']=='PASS' else 1
if __name__=='__main__':raise SystemExit(main())
