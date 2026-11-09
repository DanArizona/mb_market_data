"""Stage, verify, and install the authorization reporting fix; no live access.

Only the supplied --code-root is updated. The --data-root is read for existing
session lock names, never edited. Backups precede replacement; no session starts.
"""
from __future__ import annotations
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
from uuid import uuid4

BASE_INVENTORY_SHA='a3b575399e1e3fcd5adc137d31a6b99e0fc36b8c6219f6eb37a1a66653be9504'
IMPLEMENTATION='daily-flow-auth-reporting-20261109'
HERE=Path(__file__).resolve().parent
RUNTIME='market_data/src/mb_market_data/daily_runtime.py'
MODULE='market_data/src/mb_market_data/daily_authorization.py'
TESTS='market_data/tests/test_daily_authorization_reporting.py'
NEW_HASHES={'market_data/src/mb_market_data/daily_runtime.py': 'a5883b190a79fc19a889465a6d64d3732b82c835629492c58286414b80ab8e2f', 'market_data/src/mb_market_data/daily_authorization.py': '6f17cdea7f5cc4196033d7027fe8bcc768ba4a48b840b707a663ccf52b3f60dc', 'market_data/tests/test_daily_authorization_reporting.py': '9ca9b8c7681e35087a01868d7462a480551c1efac06b6e17dcfcd250075ffca5', 'verify_offline.py': '956593ac607328cc0722c9cfb25364a7bc9932b017eac8e565a8277e663216bd', 'SOURCE_INVENTORY.json': 'e76c83bde156029994de4b8b5b9f714d43997cb32a43624c1d01f7b29dd63ab8'}

def sha(b):return hashlib.sha256(b).hexdigest()

def safe(root,name):
    part=PurePosixPath(name)
    if part.is_absolute() or not part.parts or any(p in ('..','.') or ':' in p or '\\' in p for p in part.parts):
        raise ValueError('Unsafe source path')
    p=root.joinpath(*part.parts)
    if p.is_symlink() or not p.resolve().is_relative_to(root.resolve()):raise ValueError('Source path escapes root')
    return p

def verify(root,expected):
    raw=(root/'SOURCE_INVENTORY.json').read_bytes()
    if sha(raw)!=expected:raise ValueError('Source inventory differs; no overwrite permitted')
    j=json.loads(raw)
    for name,want in j['files'].items():
        if sha(safe(root,name).read_bytes())!=want:raise ValueError('Source file differs: '+name)
    return j

def patch_runtime(s):
    s=s.replace('from .daily_worker_liveness import OwnerHeartbeatGuard, WorkerControl','from .daily_worker_liveness import OwnerHeartbeatGuard, WorkerControl\nfrom .daily_authorization import AuthorizationFailure, failure_report, format_report, prepare_client')
    s=s.replace("IMPLEMENTATION = 'daily-flow-uptime-20261106'","IMPLEMENTATION = 'daily-flow-auth-reporting-20261109'")
    start=s.index('def prepared_client(plan, password):');end=s.index('\n\nclass BackoffClient:',start)
    s=s[:start]+'''def prepared_client(plan, password, *, reporter=None):
    return prepare_client(plan,password,auth_lock=auth_lock,reporter=reporter,clock=now)
'''+s[end:]
    s=s.replace("    if value.get('error'):lines.append('ERROR: '+value['error'])", "    if value.get('authorization'):lines.append(format_report(value['authorization']))\n    if value.get('error') and not value.get('authorization'):lines.append('ERROR: '+value['error'])")
    s=s.replace("        # Preflight credentials once in the controller before child roles start.\n        try:\n            client=prepared_client(plan,password);client.close()\n        except Exception as err:\n            value.update(phase='AUTHORIZATION_UNAVAILABLE',error=type(err).__name__,at=now().isoformat())\n            atomic_json(root/'status.json',value);atomic_text(root/'STATUS.txt',readable_status(value))\n            raise", '''        # Persist one explicit result, tied to this controller PID, before
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
            raise AuthorizationFailure(report['code'],stage=report['stage'],details=report) from None''')
    s=s.replace("    except Exception as err:\n        print('ROLE FAILED:',args.role,type(err).__name__,flush=True)\n        return 2", "    except AuthorizationFailure as err:\n        print('ROLE FAILED:',args.role,flush=True)\n        print(format_report(err.report),flush=True)\n        return 2\n    except Exception as err:\n        print('ROLE FAILED:',args.role,type(err).__name__,flush=True)\n        return 2")
    s=s.replace("    deadline=time.monotonic()+50\n    while time.monotonic()<deadline:\n        if proc.poll() is not None:\n            print('Controller exited:',proc.returncode,'Review',log);return 2\n        if owner_available(root,pid=proc.pid):", "    return wait_for_controller(proc,root,log)\n\n\ndef wait_for_controller(proc,root,log,*,timeout_seconds=50):\n    deadline=time.monotonic()+timeout_seconds\n    seen=None\n    while time.monotonic()<deadline:\n        seen=show_authorization(root,proc.pid,seen)\n        if proc.poll() is not None:\n            # Read once more after observing exit: the child may have written\n            # its receipt between the first read and this process check.\n            seen=show_authorization(root,proc.pid,seen)\n            if seen is None:print('AUTHORIZATION: UNVERIFIED - controller exited without a readable result for this launch.')\n            print('Controller exited:',proc.returncode,'Review',log);return 2\n        if owner_available(root,pid=proc.pid):\n            seen=show_authorization(root,proc.pid,seen)")
    s=s.replace("    print('Startup not confirmed. Inspect log and process status before retrying:',log)", "    print('Startup not confirmed. Do not launch another instance; inspect log and process status:',log)")
    s+='''\n\ndef show_authorization(root,pid,seen=None):
    """Never replay a prior controller's authorization result as current."""
    try:
        report=read_json(Path(root)/'authorization.json')
        if report.get('pid')!=pid or report.get('contract')!='daily-authorization-report-v1':return seen
        key=(report.get('status'),report.get('code'),report.get('observed_at'))
        if key!=seen:print(format_report(report),flush=True)
        return key
    except (OSError,ValueError,KeyError,TypeError):return seen
'''
    return s.encode('utf-8')

def payload(code):
    files={RUNTIME:patch_runtime((code/RUNTIME).read_text(encoding='utf-8')),
           MODULE:(HERE/'daily_authorization.py').read_bytes(),
           TESTS:(HERE/'test_daily_authorization_reporting.py').read_bytes()}
    v=(code/'verify_offline.py').read_text(encoding='utf-8').replace("TESTS=['test_daily_worker_uptime'","TESTS=['test_daily_authorization_reporting','test_daily_worker_uptime'").replace('EXPECTED_TESTS=177','EXPECTED_TESTS=205').replace("'uptime_targeted_tests':40", "'authorization_reporting_tests':28, 'uptime_targeted_tests':40")
    files['verify_offline.py']=v.encode('utf-8')
    inventory=json.loads((code/'SOURCE_INVENTORY.json').read_text(encoding='utf-8'))
    inventory['package']=IMPLEMENTATION
    for name,data in files.items():inventory['files'][name]=sha(data)
    inventory['authorization_reporting_correction']={'date':'2026-11-09','scope':'Nonsecret typed authorization results; foreground launch acknowledgment; no cadence or selection changes'}
    files['SOURCE_INVENTORY.json']=(json.dumps(inventory,indent=2)+'\n').encode('utf-8')
    for name,data in files.items():
        if sha(data)!=NEW_HASHES[name]:raise ValueError('Delivered correction differs: '+name)
    return files

def ensure_idle(code):
    if os.name!='nt':return
    query="$ErrorActionPreference='Stop'; @(Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','pythonw.exe') } | Select-Object ProcessId,CommandLine) | ConvertTo-Json -Compress"
    q=subprocess.run(['powershell','-NoProfile','-Command',query],capture_output=True,text=True,timeout=20)
    if q.returncode:raise RuntimeError('Process check unavailable; no code changed')
    rows=json.loads(q.stdout or '[]')
    if isinstance(rows,dict):rows=[rows]
    for row in rows:
        text=str(row.get('CommandLine') or '').casefold()
        if str(code).casefold() in text and any(x in text for x in ('run_daily_session.py','probe_universe_quote_watch.py')):
            raise RuntimeError('Daily-flow process active (PID '+str(row['ProcessId'])+'); no running-code replacement')

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--code-root',type=Path,required=True);p.add_argument('--data-root',type=Path,required=True)
    p.add_argument('--allow-test-auth-stub',action='store_true',help='Non-Windows offline development only')
    a=p.parse_args(argv)
    if a.allow_test_auth_stub and os.name=='nt':raise ValueError('Windows requires installed mb_tools import')
    code=a.code_root.expanduser().resolve();data=a.data_root.expanduser().resolve()
    if not code.is_dir() or not data.is_dir():raise ValueError('Existing code and data roots required')
    expected=NEW_HASHES['SOURCE_INVENTORY.json']
    if sha((code/'SOURCE_INVENTORY.json').read_bytes())==expected:
        verify(code,expected);print('ALREADY INSTALLED:',IMPLEMENTATION);return 0
    inv=verify(code,BASE_INVENTORY_SHA);files=payload(code)
    for name in files:
        if name not in inv['files'] and name!='SOURCE_INVENTORY.json' and safe(code,name).exists():
            raise ValueError('Unexpected existing file: '+name)
    ensure_idle(code)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:6]
    evidence=code/'output'/('auth-update-'+stamp)
    with tempfile.TemporaryDirectory(prefix='dfa_') as tmp,ExitStack() as locks:
        stage=Path(tmp)/'c';stage.mkdir()
        for name in inv['files']:
            dest=safe(stage,name);dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(safe(code,name).read_bytes())
        for name,content in files.items():
            dest=safe(stage,name);dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(content)
        verify(stage,expected)
        sys.path.insert(0,str(stage/'market_data/src'))
        from mb_market_data.acquisition_health import operation_lock
        for f in sorted((data/'output/daily_session').glob('*/plan.json')):
            plan=json.loads(f.read_text(encoding='utf-8'));journal=Path(plan['journal']).resolve()
            if not journal.is_relative_to(data/'output'):raise ValueError('Unexpected session journal path')
            key=str(journal).casefold()
            locks.enter_context(operation_lock('daily-session:'+key))
            for channel in ('uni','focus'):locks.enter_context(operation_lock('daily-channel:'+key+':'+channel))
        print('STAGED: testing authorization reporting before installation.',flush=True)
        command=[sys.executable,'-u','-B',str(stage/'verify_offline.py')]
        if a.allow_test_auth_stub:command.append('--allow-test-auth-stub')
        env=dict(os.environ);env.pop('PYTHONPATH',None)
        result=subprocess.run(command,cwd=stage,env=env,check=False)
        evidence.mkdir(parents=True,exist_ok=False)
        if (stage/'output').exists():shutil.copytree(stage/'output',evidence/'verification')
        if result.returncode:
            print('NOT INSTALLED: verification failed; existing code unchanged. Evidence:',evidence);return 1
        ensure_idle(code);verify(code,BASE_INVENTORY_SHA)
        backup=evidence/'original';backup.mkdir()
        for name in files:
            old=safe(code,name)
            if old.exists():
                dest=safe(backup,name);dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(old.read_bytes())
        applied=[]
        try:
            for name,content in files.items():
                dest=safe(code,name);dest.parent.mkdir(parents=True,exist_ok=True)
                temp=dest.with_name(dest.name+'.auth-update-'+uuid4().hex[:6])
                try:temp.write_bytes(content);os.replace(temp,dest);applied.append(name)
                finally:temp.unlink(missing_ok=True)
            verify(code,expected)
        except BaseException:
            for name in reversed(applied):
                dest=safe(code,name);old=safe(backup,name)
                if old.is_file():dest.write_bytes(old.read_bytes())
                else:dest.unlink(missing_ok=True)
            raise
        report={'result':'INSTALLED','implementation':IMPLEMENTATION,'code_root':str(code),
                'data_root_unchanged':str(data),'backup':str(backup),'updated_files':list(files),
                'tests_run':205,'credentials_accessed':False,'real_provider_calls':False,'session_started':False}
        (evidence/'installation.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(report,indent=2));print('INSTALLED:',IMPLEMENTATION)
        print('Existing daily.cmd path unchanged. No live session started.')
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as e:print('UPDATE BLOCKED:',type(e).__name__,str(e),file=sys.stderr);raise SystemExit(2)
