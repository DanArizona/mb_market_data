"""Small daily owner for existing Uni/Focus probes; no credential files.

One interactive start owns a hidden controller; it owns independent probe
children. Public plan/status/log files never include the password. Closing the
status viewer does not stop collection. Process restart is explicit and bounded
by the original session, using the retained ranking, never a reranking.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from datetime import date, datetime, timedelta, timezone
import getpass
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
from contextlib import closing
import subprocess
import sys
import time
from uuid import uuid4
import warnings

from .daily_flow import (ET, make_plan, validate_start, ensure_opening, acquire_selection,
                         publish_selection, build_next_opening, read_revision)
from .acquisition_health import HealthConfig, inspect_health, operation_lock, atomic_json, atomic_text
from .daily_worker_liveness import OwnerHeartbeatGuard, WorkerControl
from .daily_authorization import AuthorizationFailure, failure_report, format_report, prepare_client
from .daily_hot_seed import configure as configure_hot, validate_seeds, publish_seeds, seed_is_published
from .daily_attended_auth import attended_prepare
from . import daily_health

ROOT = Path(__file__).resolve().parents[2]
IMPLEMENTATION = 'daily-shared-membership-20261110'
ENTRY = ROOT / 'probes' / 'run_daily_session.py'
PROBE = ROOT / 'probes' / 'probe_universe_quote_watch.py'


def now(): return datetime.now(ET)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def secret_send(process, password):
    # Anonymous stdin pipe, not argv/env/file. Controller/child closes stdin.
    encoded = json.dumps({'password': password}).encode('utf-8')
    if not 1 <= len(encoded) <= 4096: raise ValueError('Invalid private handoff length')
    process.stdin.write(encoded); process.stdin.close()


def secret_receive():
    value = sys.stdin.buffer.read(4097)
    obj=json.loads(value)
    if len(value)>4096 or set(obj)!={'password'} or not isinstance(obj['password'],str) or not obj['password']:
        raise ValueError('Invalid private credential handoff')
    return obj['password']


def load_probe():
    spec=importlib.util.spec_from_file_location('_daily_owned_quote_probe',PROBE)
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    return module


@contextmanager
def auth_lock(ecfg):
    """Serialize client construction across owned workers (token store unchanged)."""
    deadline=time.monotonic()+40
    while True:
        manager=operation_lock('daily-auth:'+str(Path(ecfg).resolve()).casefold())
        try: manager.__enter__();break
        except ValueError:
            if time.monotonic()>=deadline: raise RuntimeError('Authentication coordination timed out')
            time.sleep(.1)
    try:yield
    finally:manager.__exit__(None,None,None)


def prepared_client(plan, password, *, reporter=None):
    return prepare_client(plan,password,auth_lock=auth_lock,reporter=reporter,clock=now)


class BackoffClient:
    """Normal network errors remain per-acquisition outcomes; 401/403 pause calls."""
    def __init__(self,client,*,state_path=None,clock=time.monotonic,stop_requested=lambda:False):
        self.client=client;self.state_path=state_path;self.clock=clock
        self.authorization_required=False;self.retry_at=0.0;self.stop_requested=stop_requested
    def report(self,state):
        if self.state_path:
            try:atomic_json(self.state_path,{'state':state,'at':now().isoformat(),'pid':os.getpid()})
            except OSError as error:
                # A status-file conflict must not turn a valid API response into
                # a request error, nor affect credential or rate-limit handling.
                print('API STATUS WRITE UNAVAILABLE:',type(error).__name__,flush=True)
    def quotes(self,*args,**kwargs):
        if self.stop_requested():raise InterruptedError('Owning session stopped')
        if self.authorization_required:
            raise RuntimeError('API paused: authorization required')
        if self.clock()<self.retry_at:
            raise RuntimeError('API paused: provider backoff')
        try:response=self.client.quotes(*args,**kwargs)
        except Exception:
            self.retry_at=self.clock()+2;self.report('TRANSIENT_ERROR')
            raise RuntimeError('Transient provider request failure') from None
        code=getattr(response,'status_code',None)
        if code in (401,403):
            self.authorization_required=True;self.report('AUTHORIZATION_REQUIRED')
        elif code==429:
            try:delay=float(getattr(response,'headers',{}).get('Retry-After',30))
            except (TypeError,ValueError):delay=30
            self.retry_at=self.clock()+min(300,max(30,delay));self.report('RATE_LIMIT_BACKOFF')
        else:self.report('AVAILABLE')
        return response
    def close(self):self.client.close()


def spawn(role, plan_path, password, log_path, *, extra=()):
    args=[sys.executable,'-u','-B',str(ENTRY),'_child','--role',role,'--plan-file',str(plan_path),'--owner-pid',str(os.getpid()),*extra]
    options={'stdin':subprocess.DEVNULL if role=='nasdaq' else subprocess.PIPE,'close_fds':True,'cwd':str(ROOT)}
    if os.name=='nt':options['creationflags']=subprocess.CREATE_NO_WINDOW
    else: options['start_new_session']=True
    with Path(log_path).open('ab',buffering=0) as log:
        proc=subprocess.Popen(args,stdout=log,stderr=log,**options)
    try:
        if role!='nasdaq':secret_send(proc,password)
    except BaseException:
        proc.terminate();proc.wait(timeout=10);raise
    return proc


def worker_argv(plan,channel,generation=0):
    output=Path(plan['state_root'])/'workers'/f'{channel}-{generation}'
    return [str(PROBE),'--watchlist-kind',channel,
        '--start-at',plan['opens_at'],'--stop-at',plan['closes_at'],
        '--journal-root',str(Path(plan['journal']).parent),'--journal-schema-version','2',
        '--cadence-profile','interleaved-v1','--ecfg',plan['ecfg'],
        '--timeout','10','--output-root',str(output)]


def owner_available(root, *, pid=None):
    """One bounded owner check for startup/commands. Workers keep a guard."""
    return OwnerHeartbeatGuard(root, pid=pid, reader=read_json,
        clock=now, monotonic=time.monotonic, sleep=time.sleep).available()


@contextmanager
def channel_lock(key, stop_requested):
    # An explicitly resumed owner may briefly overlap an old child finishing
    # its last request. Never adopt it or launch parallel channel writers.
    deadline=time.monotonic()+30
    while True:
        if stop_requested():raise InterruptedError('Owner stopped before channel lease')
        manager=operation_lock(key)
        try:manager.__enter__();break
        except ValueError:
            if time.monotonic()>=deadline:raise RuntimeError('Previous channel worker has not exited')
            time.sleep(.25)
    try:yield
    finally:manager.__exit__(None,None,None)


def run_worker(plan,password,channel,generation,owner_pid=None):
    root=Path(plan['state_root'])
    owner_pid=owner_pid if owner_pid is not None else read_json(root/'owner.json')['pid']
    control=WorkerControl(root, owner_pid=owner_pid, channel=channel,
        generation=generation, reader=read_json, clock=now)
    # The persistent guard retains only the remaining ORIGINAL heartbeat age.
    # It does not add another 30 seconds each time the file becomes unreadable.
    completion={};code=2;error_type=None
    probe=load_probe()
    argv=worker_argv(plan,channel,generation)
    old=sys.argv;sys.argv=argv
    try:
        with channel_lock('daily-channel:'+str(Path(plan['journal']).resolve()).casefold()+':'+channel,control):
            original=probe.prepare_polling_client
            def guarded(ecfg,pw,**kwargs):
                with auth_lock(ecfg):return original(ecfg,pw,**kwargs)
            probe.prepare_polling_client=guarded
            code=probe.main(supplied_password=password, stop_requested=control,
                client_transform=lambda c: BackoffClient(c,state_path=root/f'{channel}-api.json',stop_requested=control),
                on_finished=completion.update)
            if control.reason and control.reason.startswith('owner_'):
                code=3  # explicit ownership/heartbeat stop, not a user interrupt
            return code
    except Exception as error:
        error_type=type(error).__name__
        raise
    finally:
        sys.argv=old
        receipt={'contract':'daily-worker-exit-v1','implementation':IMPLEMENTATION,'at':now().isoformat(),
            'pid':os.getpid(),'channel':channel,'generation':generation,
            'owner_pid':owner_pid,'exit_code':code,
            'reason':control.reason or completion.get('stop_reason') or
                ('worker_exception' if error_type else 'probe_exit_without_completion_report'),
            'error_type':error_type,'heartbeat_read_errors':control.guard.read_errors,
            'scheduled_completion':completion.get('scheduled_completion',False),
            'probe_completion':completion}
        path=root/'workers'/f'{channel}-{generation}'/'exit.json'
        try:atomic_json(path,receipt)
        except OSError as error:
            print('WORKER EXIT RECEIPT UNAVAILABLE:',type(error).__name__,flush=True)
        print('DAILY WORKER EXIT:',json.dumps(receipt,sort_keys=True),flush=True)


def verified_resume(plan,root):
    previous=read_json(root/'plan.json')
    if previous!=plan:raise ValueError('Resume must use the original plan and opening roster')
    result_path=root/'selection'/'result.json'
    if not result_path.is_file():
        return {'status':'FOCUS_UNAVAILABLE; no saved selection','retry_selection':now()<datetime.fromisoformat(plan['selection_deadline'])}
    result=read_json(result_path)
    if result.get('proposal'):
        r1=read_revision(Path(result['proposal']))
        store=__import__('mb_market_data.quote_observation_store',fromlist=['QuoteObservationStore']).QuoteObservationStore(
            plan['journal'],session_date=date.fromisoformat(plan['session_date']),schema_version=2)
        # Identical publication is idempotent; different r1 cannot be adopted.
        store.record_membership_revision(r1)
    return result


def health_snapshot(plan,workers):
    # Owned handles, not broad process-name killing/discovery. argv describes
    # the actual delegated probe configuration; presence alone isn't progress.
    inventory={'known':True,'error':None,'processes':[
        {'pid':item['proc'].pid,'argv':item['argv']} for item in workers.values()
        if item['proc'].poll() is None]}
    config=HealthConfig(Path(plan['data_root']),Path(plan['journal']),date.fromisoformat(plan['session_date']),
        datetime.fromisoformat(plan['opens_at']),datetime.fromisoformat(plan['closes_at']),cadence_profile='interleaved-v1', channels=tuple(plan.get('channels',('uni','focus'))))
    return inspect_health(config,inventory=inventory)


TERMINAL_PHASES = {'FINISHED', 'FINISHED_WITH_FINDINGS', 'FAILED',
                   'AUTHORIZATION_UNAVAILABLE', 'EXIT_REQUESTED'}


def collection_phase(value):
    """Collection status follows observed workers and journal progress, not time."""
    phase=value.get('phase','UNKNOWN')
    if phase not in {'COLLECTING','COLLECTING_DEGRADED','COLLECTION_STOPPED','COLLECTION_UNVERIFIED'}:
        return phase
    workers=value.get('workers',{})
    alive=[v for v in workers.values() if v.get('exit_code') is None]
    health=value.get('health',{})
    channels=health.get('channels',{})
    expected={'uni'}
    if value.get('hot_activated'):expected.add('hot')
    if value.get('selection_status','').startswith(('READY','DEGRADED')):expected.add('focus')
    completed={c for c in expected if workers.get(c,{}).get('exit_code')==0 and
               channels.get(c,{}).get('state') in {'SCHEDULE_COMPLETE','WINDOW_ENDED'}}
    if workers and not alive:
        if completed==expected:
            return ('COLLECTION_COMPLETE_WITH_GAPS' if any(channels[c].get('missing_slot_count',0)
                    for c in expected) else 'COLLECTION_COMPLETE')
        return 'COLLECTION_STOPPED'
    if health.get('journal_read')=='UNAVAILABLE' or health.get('error_type'):
        return 'COLLECTION_UNVERIFIED'
    if any(v.get('exit_code') is not None and c not in completed for c,v in workers.items()):
        return 'COLLECTING_DEGRADED'
    if workers and any(c not in workers for c in expected):return 'COLLECTING_DEGRADED'
    if not channels:return 'COLLECTION_UNVERIFIED'
    for c in expected:
        info=channels.get(c,{})
        if (info.get('state') not in {'RUNNING','AWAITING_COMMIT','SCHEDULE_COMPLETE','WINDOW_ENDED'} or
                info.get('missing_slot_count',0)>0 or info.get('latest_outcomes',{}).get('request_error',0)>0 or
                workers.get(c,{}).get('api_state') in {'AUTHORIZATION_REQUIRED','RATE_LIMIT_BACKOFF','TRANSIENT_ERROR'}):
            return 'COLLECTING_DEGRADED'
    return 'COLLECTING'


def is_final_report(value):
    if (value.get('final_status') is True and value.get('phase') in TERMINAL_PHASES
            and all(v.get('exit_code') is not None for v in value.get('workers',{}).values())):return True
    # Old saved FINISHED reports predate the explicit completion marker. Do not
    # infer running processes from a historical PID in such a completed record.
    return (value.get('phase') in TERMINAL_PHASES and
            all(v.get('exit_code') is not None for v in value.get('workers',{}).values()))


def status_age_message(value, age):
    if is_final_report(value):
        return 'FINAL REPORT: session ended; saved PIDs and API results are historical, not live heartbeats.'
    if age>15:return f'WARNING: saved status is {age:.0f}s old; do not infer live progress.'
    return ''


def readable_status(value):
    lines=[f"DAILY SESSION {value['session_date']} | {collection_phase(value)}",
           f"Updated: {value['at']} | controller PID {value['pid']}",
           ('Cadence: interleaved-v1 | Hot: operator test seeds' if value.get('hot_activated') else 'Cadence: interleaved-v1 | Hot: not activated by this launcher'),
           'Selection: '+str(value.get('selection_status','WAITING FOR 09:25 ET')),
           'Next r0: '+str(value.get('next_session_status','NOT DUE'))]
    health=value.get('health',{})
    if health.get('journal_read')=='UNAVAILABLE' or health.get('error_type'):
        lines.append('HEALTH READ UNAVAILABLE: '+str(health.get('journal_error') or health.get('error_type')))
    if value.get('hot_activated'):
        lines.append('Hot seed publication: '+str(value.get('hot_status','PENDING')))
        lines.append('Independent desktop monitor: '+str(value.get('monitor_status','STARTING')))
    for name in (('uni','focus','hot') if value.get('hot_activated') else ('uni','focus')):
        h=health.get('channels',{}).get(name,{})
        # Full canonical health stays in status.json; summary uses existing fields.
        lines.append(f"{name.upper()}: {h.get('state','NOT STARTED')} | "+str(h.get('reason','')))
        if h.get('missing_slot_count',0)>0 and h.get('state')=='RUNNING':lines.append('  DEGRADED: gaps recorded; collection is continuing.')
        if h:lines.append('  '+json.dumps({k:v for k,v in h.items() if k in
            ('committed_acquisitions','missing_slot_count','last_committed_slot_utc','current_members','latest_outcomes')},default=str))
    for k,v in value.get('workers',{}).items():
        api=v.get('api_state','unknown')
        label=('last saved API status (not live): '+api if v['exit_code'] is not None else 'API '+api)
        lines.append(f"{k}: PID {v['pid']} | exit {v['exit_code']} | {label}")
        if v.get('exit_reason'):lines.append('  EXIT REASON: '+v['exit_reason'])
        if v.get('api_at'):lines.append('  API status observed: '+v['api_at'])
    for role,item in value.get('services',{}).items():
        record=item.get('status',{})
        lines.append(role.upper()+': '+str(record.get('state','STARTING'))+' | PID '+str(item.get('pid'))+' | exit '+str(item.get('exit_code')))
    if value.get('authorization'):lines.append(format_report(value['authorization']))
    if value.get('error') and not value.get('authorization'):lines.append('ERROR: '+value['error'])
    lines.append('Viewer is read-only. Stop collection and Exit session are separate commands.')
    return '\n'.join(lines)+'\n'


def run_controller(plan,password,*,resume=False):
    root=Path(plan['state_root']);plan_file=root/'plan.json';workers={};tasks={}
    schedule_close=datetime.fromisoformat(plan['closes_at'])
    deadline=datetime.fromisoformat(plan['selection_deadline'])
    post_due=schedule_close+timedelta(seconds=plan['postclose_delay_seconds'])
    post_stop=schedule_close+timedelta(seconds=plan['postclose_window_seconds'])
    value={'session_date':plan['session_date'],'pid':os.getpid(),'phase':'STARTING','implementation':IMPLEMENTATION,
           'selection_status':'WAITING FOR 09:25 ET','next_session_status':'NOT DUE'}
    channels=tuple(plan.get('channels',('uni','focus')))
    channel_generations={c:0 for c in channels}
    value['hot_activated']=plan.get('hot_activated',False)
    monitor=None
    post_started=False;selection_finished=False;stop_collection=False
    def status_write(path,payload,*,text=False):
        try:
            (atomic_text if text else atomic_json)(path,payload)
        except OSError as error:
            value['status_write_errors']=value.get('status_write_errors',0)+1
            print('STATUS WRITE UNAVAILABLE:',path.name,type(error).__name__,flush=True)
            # No claim that a failed heartbeat write succeeded. Workers retain
            # their independent 30s expiry if the owner cannot publish again.
    def publish():
        at=now().isoformat();value['at']=at
        status_write(root/'owner.json',{'pid':os.getpid(),'at':at,'active':True})
        try:value['health']=health_snapshot(plan,workers)
        except Exception as err:value['health']={'error_type':type(err).__name__}
        value['workers']={}
        for c,item in workers.items():
            api='unknown';api_at=None
            try:
                record=read_json(root/f'{c}-api.json')
                if record.get('pid')==item['proc'].pid:
                    api=record['state'];api_at=record.get('at')
            except (OSError,ValueError,KeyError,TypeError):pass
            row={'pid':item['proc'].pid,'exit_code':item['proc'].poll(),
                 'generation':item['generation'],'api_state':api,'api_at':api_at}
            if row['exit_code'] is not None:
                try:
                    exit_record=read_json(root/'workers'/f"{c}-{item['generation']}"/'exit.json')
                    if exit_record.get('pid')==row['pid']:
                        row['exit_reason']=exit_record['reason']
                        row['heartbeat_read_errors']=exit_record.get('heartbeat_read_errors',0)
                except (OSError,ValueError,KeyError,TypeError):pass
            value['workers'][c]=row
        if monitor is not None:
            value['monitor_status']='EXITED '+str(monitor.returncode) if monitor.poll() is not None else 'STARTING'
            try:
                health_state=read_json(root/'health'/'supervisor.json')
                if health_state.get('owner_pid')==os.getpid():value['monitor_status']=health_state['state'] if monitor.poll() is None else 'EXITED '+str(monitor.returncode)
            except (OSError,ValueError,TypeError,KeyError):pass
        value['services']={}
        for role in ('membership','nasdaq'):
            proc=tasks.get(role)
            if proc is not None:
                try:record=read_json(root/role/'status.json')
                except (OSError,ValueError,TypeError):record={}
                value['services'][role]={'pid':proc.pid,'exit_code':proc.poll(),
                    'status':record if record.get('pid')==proc.pid else {}}
        value['phase']=collection_phase(value)
        status_write(root/'status.json',value)
        status_write(root/'STATUS.txt',readable_status(value),text=True)
    def launch_services():
        if not plan.get('membership_control') or not selection_finished or now()>=schedule_close:
            return
        if not (root/'selection'/'publication.json').is_file():
            return
        for role in ('membership','nasdaq') if plan.get('automatic_nasdaq') else ('membership',):
            if role not in tasks:
                tasks[role]=spawn(role,plan_file,password,root/(role+'.log'))
    def restart_service(role):
        old=tasks.get(role)
        if old is not None and old.poll() is None:return
        if role=='nasdaq' and not plan.get('automatic_nasdaq'):return
        if not plan.get('membership_control') or not selection_finished or now()>=schedule_close:return
        tasks[role]=spawn(role,plan_file,password,root/(role+'.log'))
    def launch_channel(channel):
        old=workers.get(channel)
        if old and old['proc'].poll() is None:return
        generation=channel_generations[channel]
        # Resume must not reuse a prior generation's diagnostics or exit record.
        while (root/'workers'/f'{channel}-{generation}').exists():generation+=1
        channel_generations[channel]=generation+1
        (root/'workers'/f'{channel}-{generation}').mkdir(parents=True,exist_ok=False)
        proc=spawn(channel,plan_file,password,root/f'{channel}.log',extra=['--generation',str(generation)])
        workers[channel]={'proc':proc,'argv':worker_argv(plan,channel,generation),'generation':generation}
    with operation_lock('daily-session:'+str(Path(plan['journal']).resolve()).casefold()):
        validate_start(plan,now=now(),resume=resume)
        if not resume:
            if root.exists():raise ValueError('Existing daily state retained. Use explicit resume, not reset.')
            root.mkdir(parents=True,exist_ok=False);atomic_json(plan_file,plan)
        else:
            result=verified_resume(plan,root)
            value['selection_status']=result['status'];selection_finished=not result.get('retry_selection',False)
            if not selection_finished and (root/'selection').exists():
                (root/'selection').rename(root/('selection-partial-'+uuid4().hex[:8]))
            for name in ('exit.request','stop.request'):(root/name).unlink(missing_ok=True)
        ensure_opening(plan,resume=resume)
        # Persist one explicit result, tied to this controller PID, before
        # starting workers. The foreground launcher reads this receipt directly.
        def authorization_report(report):
            value['authorization']=report
            receipt=dict(report,pid=os.getpid(),session_date=plan['session_date'])
            status_write(root/'authorization.json',receipt)
            print(format_report(report),flush=True)
        try:
            client=prepared_client(plan,password,reporter=authorization_report);client.close()
        except Exception as err:
            report=failure_report(err)
            if value.get('authorization')!=report:authorization_report(report)
            value.update(phase='AUTHORIZATION_UNAVAILABLE',error=report['code'],
                         at=now().isoformat(),final_status=True,workers={})
            status_write(root/'owner.json',{'pid':os.getpid(),'at':value['at'],'active':False})
            status_write(root/'status.json',value);status_write(root/'STATUS.txt',readable_status(value),text=True)
            raise AuthorizationFailure(report['code'],stage=report['stage'],details=report) from None
        value['phase']='WAITING_FOR_SELECTION';publish()
        if resume:time.sleep(.5)
        try:
            if plan.get('independent_monitor'):
                monitor=daily_health.spawn_monitor(plan_file,owner_pid=os.getpid())
                until=time.monotonic()+12
                ready=False
                while time.monotonic()<until and monitor.poll() is None:
                    try:
                        notice=read_json(root/'health'/'ready.json')
                        ready=notice.get('owner_pid')==os.getpid() and notice.get('alarm_started') is True
                    except (OSError,ValueError,TypeError):pass
                    if ready:break
                    publish();time.sleep(.25)
                if not ready:
                    value['error']='Independent desktop monitor failed to start; inspect health/monitor.log'
                    raise RuntimeError('Independent monitor unavailable; collectors were not started')
            # Uni can be authenticated/registered before selection. Focus is
            # launched only after successful publication, so no empty first slot.
            if now()<schedule_close:launch_channel('uni')
            if resume and selection_finished:
                if result.get('proposal') and now()<schedule_close:
                    launch_channel('focus')
                    if plan.get('hot_activated') and seed_is_published(plan):
                        value['hot_status']='PUBLISHED (retained; later membership respected)';launch_channel('hot')
                    elif plan.get('hot_activated'):value['hot_status']='UNAVAILABLE; opening seeds were not published'
            else:
                tasks['selection']=spawn('selection',plan_file,password,root/'selection.log')
            while True:
                current=now()
                if (root/'exit.request').exists():
                    value['phase']='EXIT_REQUESTED';value['next_session_status']='CANCELLED BY OPERATOR';break
                if (root/'resume-collection.request').exists():
                    (root/'resume-collection.request').unlink()
                    if current<schedule_close:
                        (root/'stop.request').unlink(missing_ok=True);stop_collection=False
                        launch_channel('uni')
                        if selection_finished and (root/'selection'/'r1.json').exists():launch_channel('focus')
                        if plan.get('hot_activated') and seed_is_published(plan):launch_channel('hot')
                if (root/'stop.request').exists():stop_collection=True
                if not selection_finished:
                    proc=tasks['selection']
                    if proc.poll() is not None:
                        selection_finished=True
                        result_path=root/'selection'/'result.json'
                        if proc.returncode==0 and result_path.is_file():
                            result=read_json(result_path)
                            value['selection_status']=result['status']
                            try:
                                if publish_selection(plan,result,now=current):
                                    value['selection_status']+='; r1 PUBLISHED'
                                    if plan.get('hot_activated'):
                                        try:value['hot_status']=publish_seeds(plan,now=current)
                                        except Exception as error:
                                            value['hot_status']='UNAVAILABLE; '+type(error).__name__
                                            print('HOT SEED PUBLICATION FAILED:',type(error).__name__,flush=True)
                                    if not stop_collection:
                                        launch_channel('focus')
                                        if plan.get('hot_activated') and seed_is_published(plan):launch_channel('hot')
                            except Exception as err:
                                value['selection_status']='FOCUS UNAVAILABLE; publication '+type(err).__name__
                                atomic_json(root/'selection-publication-error.json',{'error_type':type(err).__name__,
                                    'at':current.isoformat(),'proposal_preserved':result.get('proposal')})
                        else:value['selection_status']='FOCUS UNAVAILABLE; inspect selection.log'
                    elif current>=deadline:
                        # Stop acquisition before opening; partial responses remain
                        # on disk when available. Do not start a late reranking.
                        proc.terminate();proc.wait(timeout=10)
                        selection_finished=True;value['selection_status']='FOCUS UNAVAILABLE; 09:29 deadline'
                for channel in channels:
                    request=root/f'restart-{channel}.request'
                    if request.exists():
                        request.unlink()
                        if not stop_collection and selection_finished and current<schedule_close:
                            if channel=='uni' or (channel=='focus' and value['selection_status'].startswith(('READY','DEGRADED'))) or (channel=='hot' and seed_is_published(plan)):
                                launch_channel(channel)
                launch_services()
                for role in ('membership','nasdaq'):
                    request=root/('restart-'+role+'.request')
                    if request.exists():
                        request.unlink();restart_service(role)
                # Post-close is independent of worker success and missing slots.
                if current>=post_due and not post_started:
                    post_started=True;value['next_session_status']='BUILDING'
                    tasks['postclose']=spawn('postclose',plan_file,password,root/'postclose.log')
                if post_started:
                    proc=tasks['postclose']
                    if proc.poll() is not None:
                        receipt=root/'postclose-status.json'
                        value['next_session_status']=read_json(receipt) if receipt.is_file() else 'INCOMPLETE; inspect postclose.log'
                        value['phase']='FINISHED' if isinstance(value['next_session_status'],dict) and value['next_session_status'].get('status')=='READY' else 'FINISHED_WITH_FINDINGS'
                        publish();break
                    if current>=post_stop:
                        proc.terminate();proc.wait(timeout=10)
                        value['next_session_status']='INCOMPLETE; preparation timeout; files preserved'
                        value['phase']='FINISHED_WITH_FINDINGS';publish();break
                value['phase']=('POST_CLOSE' if current>=schedule_close else
                    'COLLECTION_STOPPED; PREPARATION STILL ARMED' if stop_collection else
                    'COLLECTING' if current>=datetime.fromisoformat(plan['opens_at']) else 'WAITING_FOR_OPEN')
                publish();time.sleep(2)
        except BaseException as err:
            value['phase']='FAILED';value.setdefault('error',type(err).__name__)
            raise
        finally:
            # Stop only exact owned child handles, never process-name matching.
            atomic_text(root/'stop.request','controller cleanup\n')
            children=[w['proc'] for w in workers.values()]+list(tasks.values())
            until=time.monotonic()+12
            for proc in children:
                try:proc.wait(timeout=max(.01,until-time.monotonic()))
                except subprocess.TimeoutExpired:
                    proc.terminate();proc.wait(timeout=10)
            value['at']=now().isoformat()
            for channel,item in workers.items():
                value.setdefault('workers',{}).setdefault(channel,{})
                value['workers'][channel].update(pid=item['proc'].pid,exit_code=item['proc'].poll())
            try:value['health']=health_snapshot(plan,workers)
            except Exception:pass
            value['final_status']=True
            status_write(root/'owner.json',{'pid':os.getpid(),'at':value['at'],'active':False})
            status_write(root/'status.json',value);status_write(root/'STATUS.txt',readable_status(value),text=True)
            if monitor is not None and value['phase']!='FAILED':
                atomic_text(root/'health'/'stop.request','normal session completion or explicit exit\n')
                try:monitor.wait(timeout=12)
                except subprocess.TimeoutExpired:print('Monitor still running; no pollers restarted.',flush=True)
            password=None  # best effort only; Python does not guarantee RAM erasure
    return 0


def child_main(args):
    plan=read_json(args.plan_file);password=None if args.role=='nasdaq' else secret_receive();root=Path(plan['state_root'])
    try:
        if args.role=='controller':return run_controller(plan,password,resume=args.resume)
        if args.role in ('uni','focus','hot'):return run_worker(plan,password,args.role,args.generation,args.owner_pid)
        stop=WorkerControl(root,owner_pid=args.owner_pid,channel=args.role,generation=args.generation,
            reader=read_json,clock=now,honor_collection_stop=False)
        if args.role=='nasdaq':
            from .daily_nasdaq_source import run as source_run
            return source_run(plan,owner_pid=args.owner_pid,stop=stop)
        client=BackoffClient(prepared_client(plan,password),stop_requested=stop)
        try:
            if args.role=='membership':
                from .daily_membership import service
                return service(plan,client,owner_pid=args.owner_pid,stop=stop)
            if args.role=='selection':
                acquire_selection(client,plan,output=root/'selection',sleep=time.sleep,
                    stop_requested=stop)
                if plan.get('hot_activated'):
                    try:validate_seeds(client,plan,root/'selection',clock=now)
                    except Exception as error:
                        atomic_json(root/'selection'/'hot-seed-validation.json',{'status':'UNAVAILABLE','error_type':type(error).__name__,'requested':plan['hot_seed_symbols']})
                        print('Hot validation unavailable; ranked Focus result retained.',flush=True)
            elif args.role=='postclose':
                try:
                    result=build_next_opening(client,plan)
                    atomic_json(root/'postclose-status.json',result)
                except Exception as err:
                    atomic_json(root/'postclose-status.json',{'status':'INCOMPLETE','error_type':type(err).__name__,
                        'detail':'Review postclose output; old roster and partial evidence retained'})
                    raise
            else:raise ValueError('Unknown role')
        finally:client.close()
        return 0
    except AuthorizationFailure as err:
        print('ROLE FAILED:',args.role,flush=True)
        print(format_report(err.report),flush=True)
        return 2
    except Exception as err:
        print('ROLE FAILED:',args.role,type(err).__name__,flush=True)
        return 2
    finally:password=None


def watch(root,once=False):
    while True:
        try:
            status=read_json(root/'status.json')
            age=(now()-datetime.fromisoformat(status['at'])).total_seconds()
            if not once:
                if os.name=='nt':os.system('cls')
                else:print('\033[2J\033[H',end='')
            print(readable_status(status))
            message=status_age_message(status,age)
            if message:print(message)
        except (OSError,ValueError,KeyError):print('No readable status yet:',root)
        if once:return 0
        try:time.sleep(2)
        except KeyboardInterrupt:
            print('Viewer closed. Controller and collection unchanged.');return 0


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    for name in ('plan','start'):
        p=sub.add_parser(name);p.add_argument('--opening-proposal',type=Path,required=True)
        p.add_argument('--market-data-root',type=Path,required=True);p.add_argument('--ecfg',type=Path,required=True)
        p.add_argument('--allow-carried-roster',action='store_true')
        p.add_argument('--hot-symbols',nargs='+',help='Explicit opening Hot test seeds; enables independent three-channel desktop monitor')
        p.add_argument('--membership-controls',action='store_true',help='Enable shared manual membership worker')
        p.add_argument('--nasdaq-halts',action='store_true',help='Opt in to paced live Nasdaq LUDP/M admission through shared writer')
        if name=='start':p.add_argument('--live',action='store_true');p.add_argument('--resume',action='store_true')
    for name in ('status','watch','stop','exit','restart-uni','restart-focus','restart-hot','restart-membership','restart-nasdaq','resume-collection'):
        p=sub.add_parser(name);p.add_argument('--state-root',type=Path,required=True)
    p=sub.add_parser('_child');p.add_argument('--role',required=True);p.add_argument('--plan-file',type=Path,required=True)
    p.add_argument('--generation',type=int,default=0);p.add_argument('--resume',action='store_true');p.add_argument('--owner-pid',type=int)
    args=parser.parse_args(argv)
    if args.action=='_child':return child_main(args)
    if args.action in ('status','watch'):return watch(args.state_root.resolve(),once=args.action=='status')
    if args.action in ('stop','exit','restart-uni','restart-focus','restart-hot','restart-membership','restart-nasdaq','resume-collection'):
        root=args.state_root.resolve()
        if not owner_available(root):raise ValueError('No fresh active controller; no control request written')
        if args.action.startswith('restart-') and (root/'stop.request').exists():
            raise ValueError('Collection was stopped; use resume-collection to re-enable it')
        atomic_text(root/(args.action+'.request'),now().isoformat())
        print('Request saved. Confirm status; the request itself is not proof of completion.')
        return 0
    plan=make_plan(args.opening_proposal,args.market_data_root,args.ecfg,allow_carried=args.allow_carried_roster)
    if args.hot_symbols:plan=configure_hot(plan,args.hot_symbols)
    if args.membership_controls or args.nasdaq_halts:
        from .daily_membership import configure as configure_membership
        plan=configure_membership(plan,nasdaq=args.nasdaq_halts)
    if args.action=='plan' or not args.live:
        print(json.dumps(plan,indent=2));print('PLAN ONLY: no credential prompt, provider, or production writes.');return 0
    validate_start(plan,now=now(),resume=args.resume)
    root=Path(plan['state_root'])
    # Fast duplicate check before asking for any credentials. The controller
    # repeats the OS-owned lock check to handle simultaneous starts.
    with operation_lock('daily-session:'+str(Path(plan['journal']).resolve()).casefold()):
        if root.exists() and not args.resume:raise ValueError('Existing session. Use status or explicit resume; do not overwrite.')
    if not Path(plan['ecfg']).is_file():raise FileNotFoundError('Encrypted config not found')
    if plan.get('independent_monitor'):
        from .polling_alarm import check_desktop
        check_desktop()  # Before credentials: a live monitored session needs Tk.
    with warnings.catch_warnings():
        warnings.simplefilter('error',getpass.GetPassWarning)
        password=getpass.getpass('Encrypted config password (once for daily session): ')
    if plan.get('operator_authorization')=='foreground-attended-v1':
        with operation_lock('daily-session:'+str(Path(plan['journal']).resolve()).casefold()):
            attended_prepare(plan,password,auth_lock=auth_lock,clock=now)
        # OAuth can take time. Never mislabel a missed selection as on-time.
        validate_start(plan,now=now(),resume=args.resume)
    # Public launch ticket is in a short sibling path; no password stored.
    tickets=root.parent/'launch';tickets.mkdir(parents=True,exist_ok=True)
    ticket=tickets/(uuid4().hex[:12]+'.json');atomic_json(ticket,plan)
    log=ticket.with_suffix('.log')
    proc=spawn('controller',ticket,password,log,extra=['--resume'] if args.resume else ())
    password=None
    print('Daily controller starting; log:',log)
    return wait_for_controller(proc,root,log)


def wait_for_controller(proc,root,log,*,timeout_seconds=50):
    deadline=time.monotonic()+timeout_seconds
    seen=None
    while time.monotonic()<deadline:
        seen=show_authorization(root,proc.pid,seen)
        if proc.poll() is not None:
            # Read once more after observing exit: the child may have written
            # its receipt between the first read and this process check.
            seen=show_authorization(root,proc.pid,seen)
            if seen is None:print('AUTHORIZATION: UNVERIFIED - controller exited without a readable result for this launch.')
            print('Controller exited:',proc.returncode,'Review',log);return 2
        if owner_available(root,pid=proc.pid):
            seen=show_authorization(root,proc.pid,seen)
            print('Daily session active. State:',root)
            print('Use watch/status in any window; closing the viewer does not stop collection.')
            return watch(root)
        time.sleep(.25)
    print('Startup not confirmed. Do not launch another instance; inspect log and process status:',log)
    return 2


def show_authorization(root,pid,seen=None):
    """Never replay a prior controller's authorization result as current."""
    try:
        report=read_json(Path(root)/'authorization.json')
        if report.get('pid')!=pid or report.get('contract')!='daily-authorization-report-v1':return seen
        key=(report.get('status'),report.get('code'),report.get('observed_at'))
        if key!=seen:print(format_report(report),flush=True)
        return key
    except (OSError,ValueError,KeyError,TypeError):return seen
