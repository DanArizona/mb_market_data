"""Independent three-channel observer and alarm watchdog. No credentials/API.

The inspector reads the journal and matches actual worker command lines. The
watchdog owns the pre-existing DesktopAlarm and remains responsive if inspection
hangs or the daily controller exits. Neither process restarts or kills pollers.
"""
from __future__ import annotations
import argparse
from datetime import date, datetime, timezone
import json, os, subprocess, sys, time
from pathlib import Path
from uuid import uuid4
from . import acquisition_health as health
from .polling_alarm import DesktopAlarm, alarm_payload
from .polling_monitor_supervisor import MonitorWatchdog, stop_monitor_child
from .daily_worker_liveness import OwnerHeartbeatGuard

CHANNELS=('uni','focus','hot')
ROOT=Path(__file__).resolve().parents[2]
ENTRY=ROOT/'probes'/'run_daily_health.py'


def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def now():return datetime.now(timezone.utc)
def flag(args,name):
    try:return args[args.index(name)+1]
    except (ValueError,IndexError):return None


def delegated_inventory(plan, inventory):
    """Map only actual matching daily-worker command lines to delegated probes."""
    result=dict(inventory,processes=[])
    wanted=(Path(plan['state_root'])/'plan.json').resolve()
    for row in inventory.get('processes',[]):
        args=row.get('argv',[])
        role=flag(args,'--role');path=flag(args,'--plan-file')
        if role in CHANNELS and path and Path(path).resolve()==wanted and any(Path(s.replace('\\','/')).name=='run_daily_session.py' for s in args):
            args=['probe_universe_quote_watch.py','--watchlist-kind',role,
                  '--journal-schema-version','2','--journal-root',str(Path(plan['journal']).parent),
                  '--start-at',plan['opens_at']]
        result['processes'].append(dict(row,argv=args))
    return result


def inspect(plan, *, clock=now, inventory=None, guard=None):
    root=Path(plan['state_root']);at=clock()
    if inventory is None:inventory=health.windows_process_inventory()
    cfg=health.HealthConfig(Path(plan['data_root']),Path(plan['journal']),date.fromisoformat(plan['session_date']),
        datetime.fromisoformat(plan['opens_at']),datetime.fromisoformat(plan['closes_at']),
        cadence_profile='interleaved-v1',channels=CHANNELS)
    snapshot=health.inspect_health(cfg,now=at,inventory=delegated_inventory(plan,inventory))
    snapshot['restart_commands']={};snapshot['restart_launchers']={}
    extra={};status={}
    try:status=read(root/'status.json')
    except (OSError,ValueError,TypeError):pass
    stopped=(root/'stop.request').exists();exiting=(root/'exit.request').exists()
    final=status.get('final_status') is True and all(v.get('exit_code') is not None for v in status.get('workers',{}).values())
    active=datetime.fromisoformat(plan['opens_at'])<=at<datetime.fromisoformat(plan['closes_at'])
    if not final and not exiting and guard is not None and not guard.available():
        extra['monitor:controller_unavailable']='Daily controller heartbeat expired, unavailable or replaced; inspect owner/status. Workers are not restarted.'
    if status and not final and not exiting:
        try:age=(at-datetime.fromisoformat(status['at'])).total_seconds()
        except (KeyError,ValueError,TypeError):age=999
        if age>30:extra['monitor:controller_status_stale']='Daily controller has not published a current completed status for more than 30 seconds.'
    conditions=health.alert_conditions(snapshot)
    if not active or stopped or exiting:
        conditions={k:v for k,v in conditions.items() if k.endswith(':coverage') or k.startswith(('journal:','process:','monitor:'))}
    if active and not stopped and not exiting:
        for channel,item in snapshot['channels'].items():
            try:
                api=read(root/f'{channel}-api.json')
                matches=item.get('candidate_pids',[])
                if api.get('pid') in matches and api.get('state') in {'AUTHORIZATION_REQUIRED','RATE_LIMIT_BACKOFF'}:
                    conditions[channel+':api']=api['state']
            except (OSError,ValueError,TypeError):pass
            if item['state']=='RUNNING' and item.get('current_members')==0:
                item['reason']='Empty membership; scheduled empty-roster outcomes are being saved.'
    if active and not stopped and not exiting and plan.get('membership_control'):
        for role in ('membership','nasdaq') if plan.get('automatic_nasdaq') else ('membership',):
            entry=status.get('services',{}).get(role,{})
            try:
                report=read(root/role/'status.json')
                age=(at-datetime.fromisoformat(report['at'])).total_seconds()
                threshold=180 if role=='nasdaq' else 30
                invalid=age>threshold or report.get('state') in ('STOPPED','STALE','DEGRADED') or entry.get('exit_code') is not None
            except (OSError,ValueError,KeyError,TypeError):invalid=True
            if invalid:
                extra['monitor:'+role+'_unavailable']=role+' service is stale, stopped or degraded. Existing collectors are not stopped; inspect '+role+'.log.'
    snapshot.update(daily_conditions={**conditions,**extra},daily_active=active and not stopped and not exiting,
                    daily_final=final,daily_intentional_stop=stopped or exiting)
    return snapshot


def payload(snapshot, watchdog=None):
    value=alarm_payload(snapshot,snapshot.get('daily_conditions',{}),[],
        window_active=snapshot.get('daily_active',False),status_path=Path(snapshot['journal']).parent/'unused',
        poller_windows='the daily status viewer; leave healthy channels running')
    # Controller and observer failures are urgent even outside regular collection.
    if watchdog and watchdog['state'] in {'STOPPED','OVERDUE'}:
        key='monitor:'+watchdog['state'];value['urgent'][key]=watchdog['detail']
        value['text']=watchdog['detail']+'\n\n'+value['text']
    return value


def inspector(plan,token,owner_pid,interval=5):
    root=Path(plan['state_root']);directory=root/'health';directory.mkdir(exist_ok=True)
    guard=OwnerHeartbeatGuard(root,pid=owner_pid,reader=read,clock=now)
    while not (directory/'inspector.stop').exists():
        try:
            snapshot=inspect(plan,guard=guard)
            snapshot.update(monitor_pid=os.getpid(),monitor_token=token)
            health.atomic_json(directory/'latest.json',snapshot)
        except Exception as error:
            # A failed completed-check publication is detected by the watchdog.
            print('INSPECTOR ERROR:',type(error).__name__,flush=True)
        time.sleep(interval)
    return 0


def spawn_monitor(plan_path, *, owner_pid):
    plan=read(plan_path);root=Path(plan['state_root']);directory=root/'health';directory.mkdir(exist_ok=True)
    if (directory/'ready.json').exists():
        # Resume asks the previous independent observer to finish, without
        # trusting a stale PID or touching any quote worker.
        health.atomic_text(directory/'stop.request','new session owner\n')
        until=time.monotonic()+15
        while True:
            try:
                with health.operation_lock('three-health:'+str(Path(plan['journal']).resolve()).casefold()):pass
                break
            except ValueError:
                if time.monotonic()>=until:raise RuntimeError('Previous observer has not released its lease')
                time.sleep(.25)
    for name in ('stop.request','inspector.stop','ready.json'):(directory/name).unlink(missing_ok=True)
    options={'stdin':subprocess.DEVNULL,'close_fds':True,'cwd':str(ROOT)}
    if os.name=='nt':options['creationflags']=subprocess.CREATE_NO_WINDOW
    else:options['start_new_session']=True
    with (directory/'monitor.log').open('ab',buffering=0) as log:
        child=subprocess.Popen([sys.executable,'-u','-B',str(ENTRY),'supervise','--plan-file',str(plan_path),
            '--owner-pid',str(owner_pid)],stdout=log,stderr=log,**options)
    return child


def supervise(plan,plan_path,owner_pid):
    root=Path(plan['state_root']);directory=root/'health';directory.mkdir(exist_ok=True)
    token=uuid4().hex;child=None;desktop=None
    with health.operation_lock('three-health:'+str(Path(plan['journal']).resolve()).casefold()):
        try:
            desktop=DesktopAlarm();desktop.start()
            kwargs={'stdin':subprocess.DEVNULL,'close_fds':True}
            if os.name=='nt':kwargs['creationflags']=subprocess.CREATE_NO_WINDOW
            child=subprocess.Popen([sys.executable,'-u','-B',str(ENTRY),'inspect','--plan-file',str(plan_path),
                                    '--owner-pid',str(owner_pid),'--token',token],**kwargs)
            dog=MonitorWatchdog(child_pid=child.pid,token=token,journal=Path(plan['journal']),started_monotonic=time.monotonic())
            health.atomic_json(directory/'ready.json',{'pid':os.getpid(),'owner_pid':owner_pid,
                'at':now().isoformat(),'alarm_started':True,'channels':list(CHANNELS)})
            last=None
            while not (directory/'stop.request').exists():
                try:snapshot=read(directory/'latest.json')
                except (OSError,ValueError,TypeError):snapshot=None
                state=dog.inspect(snapshot,child_exit=child.poll(),now_monotonic=time.monotonic())
                latest=dog.last_snapshot
                if latest:
                    value=payload(latest,state)
                else:
                    urgent={'monitor:'+state['state']:state['detail']} if state['state'] in {'STOPPED','OVERDUE'} else {}
                    value={'urgent':urgent,'notices':{},'text':state['detail'],'channel_states':{},
                           'observed_at_utc':now().isoformat(),'status_path':str(root/'STATUS.txt')}
                value['status_path']=str(root/'STATUS.txt');desktop.publish(value)
                signature=json.dumps(value['urgent'],sort_keys=True)
                if signature!=last:
                    with (directory/'alerts.jsonl').open('a',encoding='utf-8') as stream:
                        stream.write(json.dumps({'at':now().isoformat(),'urgent':value['urgent']})+'\n')
                    last=signature
                try:health.atomic_json(directory/'supervisor.json',dict(state,at=now().isoformat(),pid=os.getpid(),owner_pid=owner_pid,urgent=value['urgent']))
                except OSError:pass
                time.sleep(.5)
        finally:
            if child is not None:stop_monitor_child(child,directory/'inspector.stop')
            if desktop is not None:desktop.close()
    return 0


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['inspect','supervise','alarm-test'])
    p.add_argument('--plan-file',type=Path);p.add_argument('--owner-pid',type=int);p.add_argument('--token')
    a=p.parse_args(argv)
    if a.action=='alarm-test':
        desktop=DesktopAlarm();desktop.start()
        try:
            desktop.publish({'urgent':{'hot:TEST':'TEST ONLY: Hot worker stop alert; no live process or journal.'},
                'notices':{},'text':'OFFLINE DESKTOP TEST. This window and repeating sound close automatically after 12 seconds.',
                'channel_states':{'uni':'RUNNING','focus':'RUNNING','hot':'STOPPED_EARLY'},
                'observed_at_utc':now().isoformat(),'status_path':''})
            time.sleep(12)
        finally:desktop.close()
        print('Desktop test ended. Confirm whether the window and sound appeared.');return 0
    if not a.plan_file or not a.owner_pid:raise ValueError('Exact plan and owner PID required')
    plan=read(a.plan_file)
    return inspector(plan,a.token,a.owner_pid) if a.action=='inspect' else supervise(plan,a.plan_file,a.owner_pid)

if __name__=='__main__':raise SystemExit(main())
