"""One attended unlock/renewal before any background collector is started.

Uses mb_tools' existing encrypted configuration, OAuth callback and recoverable
forced renewal. No password/token/redirect is written into session artifacts.
Background children still verify their own clients but never prompt the user.
"""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from .daily_authorization import AuthorizationFailure, _metadata, format_report, failure_report

class Discard:
    def write(self,value):return len(value)
    def flush(self):pass


def attended_prepare(plan,password,*,auth_lock,clock=None,secure_api=None,reporter=print):
    clock=clock or (lambda:datetime.now(timezone.utc));client=None;stage='dependency_import';details={}
    console_out,console_err=sys.stdout,sys.stderr
    def emit(report):reporter(format_report(report))
    try:
        if secure_api is None:import mb_tools.schwab_secure as secure_api
        required=datetime.fromisoformat(plan['closes_at'])+timedelta(seconds=plan['postclose_window_seconds'])+secure_api.DEFAULT_POLLING_REFRESH_MARGIN
        def callback(url):
            # Only the attended callback owns stdin; SDK startup output is discarded.
            with redirect_stdout(console_out),redirect_stderr(console_err):
                return secure_api.console_auth_callback(url)
        stage='auth_lock'
        with auth_lock(plan['ecfg']),redirect_stdout(Discard()),redirect_stderr(Discard()):
            stage='config_load';config=secure_api.load_secure_schwab_config(Path(plan['ecfg']),password)
            stage='token_status_before_client';status=None
            if config.tokens_db.expanduser().is_file():
                status=secure_api.read_schwab_token_status(config)
                details=_metadata(status,required,clock)
            # A still-valid refresh token can be too short for today's workload.
            # Use the established recoverable rotation rather than accepting it.
            if status is not None and clock()<status.refresh_token_expires_at<required:
                stage='attended_horizon_renewal'
                with redirect_stdout(console_out),redirect_stderr(console_err):
                    reporter('AUTHORIZATION: renewal required to cover today’s collection and post-close preparation.')
                secure_api.force_schwab_reauthorization(config,timeout=10,call_on_auth=callback)
            stage='client_initialization'
            client=secure_api.make_client_from_config(config,timeout=10,call_on_auth=callback)
            stage='token_status_after_client';status=secure_api.read_schwab_token_status(config)
            details=_metadata(status,required,clock);observed=clock()
            if status.refresh_token_expires_at<=observed:raise AuthorizationFailure('REFRESH_TOKEN_EXPIRED',stage=stage,details=details)
            if status.refresh_token_expires_at<required:raise AuthorizationFailure('REFRESH_HORIZON_INSUFFICIENT',stage=stage,details=details)
            if status.access_token_expires_at<=observed:raise AuthorizationFailure('ACCESS_TOKEN_EXPIRED',stage=stage,details=details)
            status.require_refresh_valid_through(required)
        report={'contract':'daily-authorization-report-v1','status':'VERIFIED','code':'PERSISTED_TOKEN_LIFETIMES_VALID',
                'stage':stage,'message':'Attended authorization complete; persisted token lifetimes cover today’s session.',**details}
        emit(report);return report
    except Exception as error:
        if isinstance(error,AuthorizationFailure):failure=error
        else:
            name=type(error).__name__
            if name in {'EcfgDecryptError','InvalidToken'}:code='CONFIG_DECRYPT_FAILED'
            elif stage=='config_load':code='CONFIG_UNAVAILABLE' if isinstance(error,OSError) else 'CONFIG_INVALID'
            elif stage.startswith('token_status'):code='TOKEN_STATUS_UNAVAILABLE'
            elif stage=='dependency_import' or name=='SchwabdevNotInstalledError':code='SDK_UNAVAILABLE'
            elif name=='SchwabdevVersionError':code='SDK_UNSUPPORTED'
            elif stage=='auth_lock':code='AUTH_COORDINATION_TIMEOUT'
            else:code='CLIENT_INITIALIZATION_FAILED'
            failure=AuthorizationFailure(code,stage=stage,details=details,error_type=name)
        emit(failure.report);raise failure from None
    except KeyboardInterrupt:
        reporter('AUTHORIZATION: CANCELLED. No daily controller was started.');raise
    finally:
        if client is not None:
            try:
                with redirect_stdout(Discard()),redirect_stderr(Discard()):client.close()
            except Exception:pass
