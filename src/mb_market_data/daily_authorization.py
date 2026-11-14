"""Nonsecret authorization receipts for the daily launcher.

No SDK exception text, token values, config contents, or redirect URLs are
persisted. VERIFIED means persisted lifetimes were checked after client setup;
it does not claim that a market-data request or the daily session succeeded.
"""
from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path

CONTRACT = "daily-authorization-report-v1"
MESSAGES = {
    "CONFIG_DECRYPT_FAILED": ("The encrypted configuration could not be decrypted; the password may be wrong or the file damaged.", "Check the .ecfg password and file; token renewal does not fix decryption."),
    "CONFIG_UNAVAILABLE": ("The encrypted configuration could not be opened.", "Check the configured .ecfg path and file permissions."),
    "CONFIG_INVALID": ("The decrypted configuration is missing or has invalid required settings.", "Check the Schwab configuration locally; do not paste secret values."),
    "TOKEN_STATUS_UNAVAILABLE": ("Persisted token metadata could not be read or validated.", "Run mb-schwab-auth --status in this environment; do not delete the token database."),
    "REFRESH_TOKEN_EXPIRED": ("Schwab refresh authorization has expired.", "Run mb-schwab-auth to complete attended authorization, then explicitly resume the daily session."),
    "REFRESH_HORIZON_INSUFFICIENT": ("Refresh authorization does not cover the required collection and post-close window.", "Before starting clients, use mb-schwab-auth --force-reauthorize, then explicitly resume the daily session."),
    "BROWSER_AUTHORIZATION_REQUIRED": ("Schwab requested interactive authorization; background workers cannot complete it.", "Run mb-schwab-auth in a console, then explicitly resume the daily session."),
    "ACCESS_TOKEN_EXPIRED": ("The saved access token is still expired after client initialization.", "Run mb-schwab-auth and check its result before resuming."),
    "AUTH_COORDINATION_TIMEOUT": ("Timed out waiting for another client to finish credential setup.", "Check for another authorization process; do not start duplicate daily controllers."),
    "SDK_UNAVAILABLE": ("The required Schwab client package could not be imported.", "Check the active Conda environment and its installed Schwab dependencies."),
    "SDK_UNSUPPORTED": ("The installed Schwab client version is unsupported by mb_tools.", "Check the active environment's Schwabdev version."),
    "CLIENT_INITIALIZATION_FAILED": ("Schwab client initialization failed; persisted authorization was not verified.", "Check connectivity and run mb-schwab-auth; do not assume the password was wrong."),
    "CREDENTIAL_PREPARATION_FAILED": ("Credential preparation failed for an unclassified reason.", "Review the recorded stage and error type; no authorization success is claimed."),
}

class AuthorizationFailure(RuntimeError):
    def __init__(self, code, *, stage="credential_preflight", details=None, error_type=None):
        if code not in MESSAGES: code="CREDENTIAL_PREPARATION_FAILED"
        message, action=MESSAGES[code]
        self.report={"contract":CONTRACT,"status":"FAILED","code":code,"stage":stage,
                     "message":message,"action":action,"observed_at":datetime.now(timezone.utc).isoformat()}
        if error_type: self.report["error_type"]=error_type
        if details: self.report.update(details)
        super().__init__(message)


def failure_report(error):
    if isinstance(error,AuthorizationFailure): return dict(error.report)
    return AuthorizationFailure("CREDENTIAL_PREPARATION_FAILED",error_type=type(error).__name__).report


def format_report(report):
    """Only our allowlisted fields are rendered; raw exceptions are never used."""
    lines=["AUTHORIZATION: "+report["status"]+" ["+report["code"]+"]",report["message"]]
    for key,label in (("stage","Stage"),("error_type","Error type"),
                      ("refresh_expires_at","Refresh expires"),("access_expires_at","Access expires"),
                      ("required_through","Required through"),("action","Next action")):
        if report.get(key): lines.append(label+": "+str(report[key]))
    return "\n".join(lines)


def _metadata(status, required, clock):
    values={"refresh_expires_at":status.refresh_token_expires_at,
            "access_expires_at":status.access_token_expires_at,"required_through":required,
            "observed_at":clock()}
    if any(not isinstance(v,datetime) or v.utcoffset() is None for v in values.values()):
        raise ValueError("Aware token timestamps required")
    return {k:v.isoformat() for k,v in values.items()}


def prepare_client(plan, password, *, auth_lock, reporter=None, clock=None, secure_api=None):
    """Create the client, then re-read its saved token metadata before success."""
    clock=clock or (lambda:datetime.now(timezone.utc))
    def emit(report):
        if reporter is not None: reporter(dict(report))
    stage="dependency_import"; client=None; required=None; details={}
    class NoBrowser:
        requested=False
        def __call__(self,url):self.requested=True;return ""
    rejection=NoBrowser()
    try:
        if secure_api is None:
            import mb_tools.schwab_secure as secure_api
        required=datetime.fromisoformat(plan["closes_at"])+timedelta(seconds=plan["postclose_window_seconds"])+secure_api.DEFAULT_POLLING_REFRESH_MARGIN
        stage="auth_lock"
        # SDK output can contain OAuth redirect URLs. Only our receipts escape.
        with auth_lock(plan["ecfg"]), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            stage="config_load"
            config=secure_api.load_secure_schwab_config(Path(plan["ecfg"]),password)
            stage="token_status_before_client"
            status=secure_api.read_schwab_token_status(config)
            details=_metadata(status,required,clock)
            if status.refresh_token_expires_at<=datetime.fromisoformat(details["observed_at"]):
                raise AuthorizationFailure("REFRESH_TOKEN_EXPIRED",stage=stage,details=details)
            if status.refresh_token_expires_at<required:
                raise AuthorizationFailure("REFRESH_HORIZON_INSUFFICIENT",stage=stage,details=details)
            status.require_refresh_valid_through(required)
            stage="client_initialization"
            client=secure_api.make_client_from_config(config,timeout=10,call_on_auth=rejection)
            if rejection.requested:
                raise AuthorizationFailure("BROWSER_AUTHORIZATION_REQUIRED",stage=stage,details=details)
            stage="token_status_after_client"
            status=secure_api.read_schwab_token_status(config)
            details=_metadata(status,required,clock)
            observed=datetime.fromisoformat(details["observed_at"])
            if status.refresh_token_expires_at<=observed:
                raise AuthorizationFailure("REFRESH_TOKEN_EXPIRED",stage=stage,details=details)
            if status.refresh_token_expires_at<required:
                raise AuthorizationFailure("REFRESH_HORIZON_INSUFFICIENT",stage=stage,details=details)
            status.require_refresh_valid_through(required)
            if status.access_token_expires_at<=observed:
                raise AuthorizationFailure("ACCESS_TOKEN_EXPIRED",stage=stage,details=details)
        emit({"contract":CONTRACT,"status":"VERIFIED","code":"PERSISTED_TOKEN_LIFETIMES_VALID",
              "stage":stage,"message":"Encrypted configuration accepted; client initialized; persisted token lifetimes verified.",
              "scope":"Local token metadata and client initialization; no market-data probe or session-success claim.",**details})
        return client
    except Exception as error:
        if client is not None:
            try:
                with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):client.close()
            except Exception: pass
        if isinstance(error,AuthorizationFailure): failure=error
        else:
            name=type(error).__name__
            if name in {"EcfgDecryptError","InvalidToken"}: code="CONFIG_DECRYPT_FAILED"
            elif stage=="config_load": code="CONFIG_UNAVAILABLE" if isinstance(error,OSError) else "CONFIG_INVALID"
            elif stage=="auth_lock": code="AUTH_COORDINATION_TIMEOUT"
            elif name=="SchwabdevVersionError": code="SDK_UNSUPPORTED"
            elif stage=="dependency_import" or name=="SchwabdevNotInstalledError": code="SDK_UNAVAILABLE"
            elif stage.startswith("token_status"): code="TOKEN_STATUS_UNAVAILABLE"
            elif rejection.requested: code="BROWSER_AUTHORIZATION_REQUIRED"
            elif stage=="client_initialization": code="CLIENT_INITIALIZATION_FAILED"
            else: code="CREDENTIAL_PREPARATION_FAILED"
            failure=AuthorizationFailure(code,stage=stage,details=details,error_type=name)
        emit(failure.report)
        raise failure from None
