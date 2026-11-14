"""Actual probe process with a synthetic clock/client. Network is denied.

Invoked ONLY by offline tests. It is not an alternative live launcher.
"""
from pathlib import Path
import sys,json,socket,getpass,os
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).resolve().parent)]
from daily_flow_auth_stub import install
install()
def deny(*a,**k):raise AssertionError('Offline process network/password denied')
socket.socket.connect=socket.socket.connect_ex=socket.create_connection=socket.getaddrinfo=deny
getpass.getpass=deny
from mb_market_data import daily_runtime as rt
from mb_market_data import schwab_quotes as sq
import mb_tools.schwab_secure as auth
from test_volume_proxy_experiment import Response
plan=json.loads(Path(sys.argv[1]).read_text());channel=sys.argv[2]
mode=sys.argv[3] if len(sys.argv)>3 else 'healthy'
private=rt.secret_receive()
assert private=='OFFLINE-CANARY-NOT-A-REAL-PASSWORD'
clock=[datetime.fromisoformat(plan['opens_at'])-timedelta(milliseconds=100)]
class Clock(datetime):
    @classmethod
    def now(cls,tz=None):return clock[0].astimezone(tz or timezone.utc)
class Client:
    def quotes(self,symbols,*,fields):
        return Response({s:{'symbol':s,'assetMainType':'EQUITY','quote':{'lastPrice':2,'totalVolume':100}}
                         for s in symbols})
    def close(self):pass
status=SimpleNamespace(observed_at=clock[0],refresh_token_expires_at=clock[0]+timedelta(days=3),
                       require_refresh_valid_through=lambda end:None)
auth.load_secure_schwab_config=lambda path,pw:{}
auth.read_schwab_token_status=lambda *a,**k:status
auth.make_client_from_config=lambda *a,**k:Client()
probe=rt.load_probe();probe.datetime=Clock;sq.datetime=Clock
# The real poller scheduler advances synthetic time; journal publication was
# already done by the parent. Actual subprocess and SQLite writes are used.
import time
probe.time_module=SimpleNamespace(monotonic=time.monotonic,
    sleep=lambda seconds:clock.__setitem__(0,clock[0]+timedelta(seconds=seconds)))
rt.now=lambda:clock[0];rt.load_probe=lambda:probe
# Exercise the REAL liveness guard. Only the source heartbeat timestamps and
# selected I/O failures are synthetic; no always-True liveness substitution.
read_json=rt.read_json;owner_reads=[0]
root=Path(plan['state_root'])
owner_pid=read_json(root/'owner.json')['pid']
initial_at=read_json(root/'owner.json')['at']
def owner_reader(path):
    if Path(path).name!='owner.json':return read_json(path)
    owner_reads[0]+=1
    if mode=='read-glitch' and owner_reads[0] in range(8,12):
        raise PermissionError(13,'synthetic Windows-style sharing read')
    value=read_json(path)
    value['at']=initial_at if mode=='expired' else clock[0].isoformat()
    if clock[0]>=datetime.fromisoformat(plan['opens_at'])+timedelta(seconds=1):
        if mode=='operator-stop':(root/'stop.request').write_text('synthetic explicit stop')
        if mode=='replaced':value['pid']=owner_pid+1
        if mode=='inactive':value['active']=False
    return value
rt.read_json=owner_reader
code=rt.run_worker(plan,private,channel,0,owner_pid)

print(json.dumps({'result':'PROCESS_COMPLETE','channel':channel,'code':code,
                  'synthetic_api':True,'password_written':False}))
raise SystemExit(code)
