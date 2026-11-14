"""Offline actual watchdog/inspector process fixture; no Tk, credentials or API."""
from pathlib import Path
import sys,json,os,time,socket,getpass
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from mb_market_data import daily_health as dh
from mb_market_data.acquisition_health import atomic_json

def deny(*a,**k):raise AssertionError('No network or credentials in health fixture')
socket.socket.connect=socket.create_connection=socket.getaddrinfo=getpass.getpass=deny
if sys.argv[1]=='inspect':
    plan=dh.read(sys.argv[sys.argv.index('--plan-file')+1]);root=Path(plan['state_root'])/'health'
    token=sys.argv[sys.argv.index('--token')+1]
    snapshot={'journal':plan['journal'],'monitor_pid':os.getpid(),'monitor_token':token,
              'observed_at_utc':dh.now().isoformat(),'daily_conditions':{},'daily_active':True,
              'channels':{c:{'state':'RUNNING','missing_slot_count':0} for c in ('uni','focus','hot')}}
    atomic_json(root/'latest.json',snapshot);time.sleep(.2);raise SystemExit(7)
plan_path=Path(sys.argv[2]);plan=dh.read(plan_path);root=Path(plan['state_root'])/'health'
class Alarm:
    def start(self):pass
    def close(self):pass
    def publish(self,value):
        if 'monitor:STOPPED' in value['urgent']:
            atomic_json(root/'alert-observed.json',value)
            (root/'stop.request').write_text('offline fixture complete')
dh.DesktopAlarm=Alarm;dh.ENTRY=Path(__file__).resolve()
raise SystemExit(dh.supervise(plan,plan_path,os.getpid()))
