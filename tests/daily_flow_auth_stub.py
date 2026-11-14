"""OFFLINE TEST ONLY: import surface when mb_tools is absent on Linux.
No decryption, client, tokens or provider are implemented. Tests must inject them.
Never installed or imported by run_daily_session.py.
"""
import sys, types
from datetime import timedelta

def install():
    if 'mb_tools.schwab_secure' in sys.modules:return
    try:import mb_tools.schwab_secure;return
    except ImportError:pass
    parent=types.ModuleType('mb_tools');parent.__path__=[]
    module=types.ModuleType('mb_tools.schwab_secure')
    def denied(*a,**k):raise AssertionError('Synthetic authentication boundary was not substituted')
    for key in ('load_secure_schwab_config','make_client_from_config','read_schwab_token_status',
                'make_secure_schwab_client','console_auth_callback'):
        setattr(module,key,denied)
    module.DEFAULT_POLLING_REFRESH_MARGIN=timedelta(minutes=75,seconds=30)
    module.SchwabTokenStatus=object
    parent.schwab_secure=module
    sys.modules['mb_tools']=parent;sys.modules['mb_tools.schwab_secure']=module
