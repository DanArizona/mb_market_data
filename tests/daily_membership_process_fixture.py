"""OFFLINE ONLY: real collector process, fake provider/clock, parent barriers."""
from pathlib import Path
import sys,os,json,socket,getpass,time
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).parent)]
from daily_flow_auth_stub import install
install()
def denied(*a,**k):raise AssertionError('Offline fixture: no network/password')
socket.socket.connect=socket.socket.connect_ex=socket.create_connection=socket.getaddrinfo=denied
getpass.getpass=denied
from mb_market_data import daily_runtime as rt,schwab_quotes as sq
import mb_tools.schwab_secure as auth
from test_volume_proxy_experiment import Response
plan=json.loads(Path(sys.argv[1]).read_text());channel=sys.argv[2]
password=rt.secret_receive();assert password=='OFFLINE-NOT-A-REAL-PASSWORD'
root=Path(plan['state_root']);start=datetime.fromisoformat(plan['opens_at']);clock=[start-timedelta(milliseconds=100)]
class Clock(datetime):
 @classmethod
 def now(cls,tz=None):return clock[0].astimezone(tz or timezone.utc)
class Client:
 def quotes(self,symbols,*,fields):return Response({s:{'symbol':s,'assetMainType':'EQUITY','quote':{'lastPrice':100+(clock[0]-start).total_seconds()/10,'totalVolume':1000}} for s in symbols})
 def close(self):pass
auth.load_secure_schwab_config=lambda *a,**k:{}
auth.read_schwab_token_status=lambda *a,**k:SimpleNamespace(observed_at=clock[0],refresh_token_expires_at=clock[0]+timedelta(days=3),require_refresh_valid_through=lambda *_:None)
auth.make_client_from_config=lambda *a,**k:Client()
probe=rt.load_probe();probe.datetime=Clock;sq.datetime=Clock
passed=set()
def sleep(seconds):
 target=clock[0]+timedelta(seconds=seconds)
 for barrier in (12,24,36):
  bound=start+timedelta(seconds=barrier)
  if barrier not in passed and clock[0]<=bound<=target:
   clock[0]=bound;passed.add(barrier)
   (root/f'barrier-{barrier}-{channel}.ready').write_text(str(os.getpid()))
   end=time.monotonic()+20
   while not (root/f'barrier-{barrier}.release').exists():
    if time.monotonic()>end:raise RuntimeError('Offline parent barrier timeout')
    time.sleep(.01)
 clock[0]=target
probe.time_module=SimpleNamespace(monotonic=time.monotonic,sleep=sleep);rt.now=lambda:clock[0];rt.load_probe=lambda:probe
original=rt.read_json;pid=original(root/'owner.json')['pid']
def read(path):
 v=original(path)
 if Path(path).name=='owner.json':v['at']=clock[0].isoformat()
 return v
rt.read_json=read
code=rt.run_worker(plan,password,channel,0,pid)
print(json.dumps({'channel':channel,'pid':os.getpid(),'code':code,'barriers':sorted(passed),'provider':'synthetic'}))
raise SystemExit(code)
