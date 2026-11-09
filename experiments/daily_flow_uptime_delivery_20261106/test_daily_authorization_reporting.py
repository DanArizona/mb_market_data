"""Offline tests: SDK boundaries are fake; controller persistence is real."""
from contextlib import contextmanager, nullcontext, redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import io, json, os, subprocess, sys, tempfile, unittest
from mb_market_data import daily_authorization as au, daily_runtime as rt

UTC=timezone.utc
CANARY='NEVER-LOG-PASSWORD-OR-OAUTH-CODE'
class AuthorizationReportingTests(unittest.TestCase):
    def setUp(self):
        self.at=datetime(2026,11,9,14,15,tzinfo=UTC)
        self.plan={'closes_at':'2026-11-09T16:00:00-05:00','postclose_window_seconds':900,'ecfg':'unused.ecfg'}
        self.client=Mock();self.receipts=[]
        self.api=SimpleNamespace(DEFAULT_POLLING_REFRESH_MARGIN=timedelta(minutes=75,seconds=30),
            load_secure_schwab_config=Mock(return_value=object()),
            read_schwab_token_status=Mock(side_effect=[self.status(),self.status()]),
            make_client_from_config=Mock(return_value=self.client))
    def status(self,refresh=None,access=None):
        return SimpleNamespace(refresh_token_expires_at=refresh or self.at+timedelta(days=7),
            access_token_expires_at=access or self.at+timedelta(minutes=30),
            require_refresh_valid_through=Mock())
    def prepare(self,lock=lambda _:nullcontext()):
        return au.prepare_client(self.plan,CANARY,auth_lock=lock,reporter=self.receipts.append,
                                 clock=lambda:self.at,secure_api=self.api)
    def failure(self,code):
        out,err=io.StringIO(),io.StringIO()
        with redirect_stdout(out),redirect_stderr(err),self.assertRaises(au.AuthorizationFailure) as found:
            self.prepare()
        self.assertEqual(found.exception.report['code'],code)
        self.assertEqual(self.receipts[-1]['status'],'FAILED')
        self.assertNotIn(CANARY,json.dumps(self.receipts)+out.getvalue()+err.getvalue()+str(found.exception))
        return found.exception.report
    def test_refresh_expired_stops_before_client_and_reports_expiry(self):
        self.api.read_schwab_token_status.side_effect=[self.status(refresh=self.at-timedelta(days=2))]
        report=self.failure('REFRESH_TOKEN_EXPIRED')
        self.api.make_client_from_config.assert_not_called()
        self.assertIn('refresh_expires_at',report);self.assertIn('mb-schwab-auth',report['action'])
    def test_refresh_equal_now_is_expired(self):
        self.api.read_schwab_token_status.side_effect=[self.status(refresh=self.at)]
        self.failure('REFRESH_TOKEN_EXPIRED')
    def test_valid_but_insufficient_horizon_is_distinct(self):
        self.api.read_schwab_token_status.side_effect=[self.status(refresh=self.at+timedelta(hours=1))]
        report=self.failure('REFRESH_HORIZON_INSUFFICIENT')
        self.assertIn('required_through',report);self.api.make_client_from_config.assert_not_called()
    def test_success_requires_fresh_persisted_read(self):
        self.assertIs(self.prepare(),self.client)
        self.assertEqual(self.api.read_schwab_token_status.call_count,2)
        self.assertEqual(self.receipts[-1]['status'],'VERIFIED');self.client.close.assert_not_called()
    def test_expired_access_before_initialization_can_refresh(self):
        self.api.read_schwab_token_status.side_effect=[self.status(access=self.at-timedelta(minutes=1)),self.status()]
        self.prepare();self.assertEqual(self.receipts[-1]['status'],'VERIFIED')
    def test_expired_access_after_initialization_is_failure(self):
        self.api.read_schwab_token_status.side_effect=[self.status(),self.status(access=self.at-timedelta(seconds=1))]
        self.failure('ACCESS_TOKEN_EXPIRED');self.client.close.assert_called_once()
    def test_refresh_can_expire_after_initialization(self):
        self.api.read_schwab_token_status.side_effect=[self.status(),self.status(refresh=self.at-timedelta(seconds=1))]
        self.failure('REFRESH_TOKEN_EXPIRED');self.client.close.assert_called_once()
    def test_decryption_failure_does_not_misdiagnose_expiry(self):
        error=type('EcfgDecryptError',(Exception,),{})
        self.api.load_secure_schwab_config.side_effect=error(CANARY)
        self.failure('CONFIG_DECRYPT_FAILED');self.api.read_schwab_token_status.assert_not_called()
    def test_missing_config(self):
        self.api.load_secure_schwab_config.side_effect=FileNotFoundError(CANARY)
        self.failure('CONFIG_UNAVAILABLE')
    def test_invalid_config(self):
        self.api.load_secure_schwab_config.side_effect=ValueError(CANARY)
        self.failure('CONFIG_INVALID')
    def test_unreadable_metadata_before_client(self):
        self.api.read_schwab_token_status.side_effect=RuntimeError(CANARY)
        self.failure('TOKEN_STATUS_UNAVAILABLE')
    def test_unreadable_metadata_after_client_closes_client(self):
        self.api.read_schwab_token_status.side_effect=[self.status(),RuntimeError(CANARY)]
        self.failure('TOKEN_STATUS_UNAVAILABLE');self.client.close.assert_called_once()
    def test_naive_metadata_not_verified(self):
        s=self.status();s.refresh_token_expires_at=datetime(2026,11,15)
        self.api.read_schwab_token_status.side_effect=[s]
        self.failure('TOKEN_STATUS_UNAVAILABLE')
    def test_sdk_absence(self):
        self.api.make_client_from_config.side_effect=type('SchwabdevNotInstalledError',(ImportError,),{})(CANARY)
        self.failure('SDK_UNAVAILABLE')
    def test_sdk_version(self):
        self.api.make_client_from_config.side_effect=type('SchwabdevVersionError',(RuntimeError,),{})(CANARY)
        self.failure('SDK_UNSUPPORTED')
    def test_browser_request_never_verified(self):
        def factory(*a,**kw):kw['call_on_auth']('https://invalid/?code='+CANARY);return self.client
        self.api.make_client_from_config.side_effect=factory
        self.failure('BROWSER_AUTHORIZATION_REQUIRED');self.client.close.assert_called_once()
    def test_client_failure_suppresses_sdk_output_and_exception(self):
        def factory(*a,**kw):print(CANARY);print(CANARY,file=sys.stderr);raise RuntimeError(CANARY)
        self.api.make_client_from_config.side_effect=factory
        self.failure('CLIENT_INITIALIZATION_FAILED')
    def test_lock_timeout(self):
        @contextmanager
        def lock(_):raise RuntimeError(CANARY);yield
        with self.assertRaises(au.AuthorizationFailure) as caught:self.prepare(lock)
        self.assertEqual(caught.exception.report['code'],'AUTH_COORDINATION_TIMEOUT')
        self.assertNotIn(CANARY,str(caught.exception))
    def test_cleanup_failure_preserves_primary_failure(self):
        self.api.read_schwab_token_status.side_effect=[self.status(),RuntimeError(CANARY)]
        self.client.close.side_effect=RuntimeError(CANARY)
        self.failure('TOKEN_STATUS_UNAVAILABLE')
    def test_unknown_exception_report_never_contains_secret(self):
        report=au.failure_report(RuntimeError(CANARY))
        self.assertEqual(report['code'],'CREDENTIAL_PREPARATION_FAILED')
        self.assertNotIn(CANARY,au.format_report(report))

class ReportingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
    def report(self,status='FAILED',pid=500):
        r=au.AuthorizationFailure('REFRESH_TOKEN_EXPIRED').report
        if status=='VERIFIED':r.update(status=status,code='PERSISTED_TOKEN_LIFETIMES_VALID',message='Persisted token lifetimes verified.')
        return dict(r,pid=pid,session_date='2026-11-09')
    def test_controller_failure_saved_and_no_children_started(self):
        import test_daily_runtime
        fixture=test_daily_runtime.RuntimeTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        failure=au.AuthorizationFailure('REFRESH_TOKEN_EXPIRED',details={'refresh_expires_at':'2026-11-06T23:16:18+00:00'})
        with patch.object(rt,'now',fixture.clock),patch.object(rt,'prepared_client',side_effect=failure), \
             patch.object(rt,'spawn') as spawn,redirect_stdout(io.StringIO()):
            with self.assertRaises(au.AuthorizationFailure):rt.run_controller(fixture.plan,CANARY)
        spawn.assert_not_called()
        saved=rt.read_json(fixture.state/'status.json')
        self.assertEqual(saved['phase'],'AUTHORIZATION_UNAVAILABLE')
        self.assertEqual(saved['authorization']['code'],'REFRESH_TOKEN_EXPIRED')
        self.assertFalse(rt.read_json(fixture.state/'owner.json')['active'])
        self.assertIn('REFRESH_TOKEN_EXPIRED',(fixture.state/'STATUS.txt').read_text())
        self.assertNotIn(CANARY,json.dumps(saved))
    def test_saved_success_visible_in_status(self):
        v={'session_date':'2026-11-09','at':'2026-11-09T09:15:00-05:00','pid':500,
           'phase':'WAITING_FOR_SELECTION','authorization':self.report('VERIFIED')}
        self.assertIn('AUTHORIZATION: VERIFIED',rt.readable_status(v))
    def test_failure_is_printed_before_parent_exits(self):
        rt.atomic_json(self.root/'authorization.json',self.report())
        proc=Mock(pid=500,returncode=2);proc.poll.return_value=2
        out=io.StringIO()
        with redirect_stdout(out):code=rt.wait_for_controller(proc,self.root,self.root/'launch.log')
        self.assertEqual(code,2);self.assertIn('AUTHORIZATION: FAILED [REFRESH_TOKEN_EXPIRED]',out.getvalue())
        self.assertIn('mb-schwab-auth',out.getvalue());self.assertNotIn('RuntimeError',out.getvalue())
    def test_parent_never_uses_previous_controller_receipt(self):
        rt.atomic_json(self.root/'authorization.json',self.report('VERIFIED',pid=499))
        proc=Mock(pid=500,returncode=2);proc.poll.return_value=2
        out=io.StringIO()
        with redirect_stdout(out):rt.wait_for_controller(proc,self.root,self.root/'launch.log')
        self.assertIn('AUTHORIZATION: UNVERIFIED',out.getvalue());self.assertNotIn('AUTHORIZATION: VERIFIED',out.getvalue())
    def test_verified_is_printed_before_viewer(self):
        rt.atomic_json(self.root/'authorization.json',self.report('VERIFIED'))
        proc=Mock(pid=500);proc.poll.return_value=None
        out=io.StringIO()
        with redirect_stdout(out),patch.object(rt,'owner_available',return_value=True),patch.object(rt,'watch',return_value=0) as view:
            self.assertEqual(rt.wait_for_controller(proc,self.root,self.root/'launch.log'),0)
        view.assert_called_once();self.assertIn('AUTHORIZATION: VERIFIED',out.getvalue())
    def test_read_failure_remains_unknown_not_success(self):
        with patch.object(rt,'read_json',side_effect=PermissionError(CANARY)),redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(rt.show_authorization(self.root,500))
        self.assertEqual(out.getvalue(),'')
    def test_same_result_not_repeated_every_poll(self):
        rt.atomic_json(self.root/'authorization.json',self.report())
        out=io.StringIO()
        with redirect_stdout(out):
            seen=rt.show_authorization(self.root,500);rt.show_authorization(self.root,500,seen)
        self.assertEqual(out.getvalue().count('AUTHORIZATION: FAILED'),1)
    def test_actual_child_failure_receipt_reaches_foreground(self):
        # Real subprocess, synthetic credentials/provider boundary; no mb_tools/vault.
        script=self.root/'child.py'
        script.write_text('''import sys\nfrom pathlib import Path\nfrom mb_market_data import daily_runtime as rt, daily_authorization as au\np=Path(sys.argv[1])\nr=dict(au.AuthorizationFailure("REFRESH_TOKEN_EXPIRED").report,pid=__import__("os").getpid())\nrt.atomic_json(p/"authorization.json",r)\nraise SystemExit(2)\n''')
        env=dict(os.environ,PYTHONPATH=str(Path(rt.__file__).parents[1]))
        proc=subprocess.Popen([sys.executable,'-B',str(script),str(self.root)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
        out=io.StringIO()
        try:
            with redirect_stdout(out):code=rt.wait_for_controller(proc,self.root,self.root/'child.log',timeout_seconds=10)
            self.assertEqual(code,2);self.assertIn('REFRESH_TOKEN_EXPIRED',out.getvalue())
        finally:proc.communicate(timeout=10)

if __name__=='__main__':unittest.main(verbosity=2)
