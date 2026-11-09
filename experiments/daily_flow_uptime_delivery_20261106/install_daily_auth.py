"""Verify and install a small daily-flow authorization reporting correction.

Uses only local source/payloads. Does not unlock credentials, call providers,
start a daily session, change mb_tools, or edit collected/session data.
"""
from __future__ import annotations
import argparse
import base64
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
from uuid import uuid4
import zipfile

BASE_INVENTORY_SHA = 'a3b575399e1e3fcd5adc137d31a6b99e0fc36b8c6219f6eb37a1a66653be9504'
PAYLOAD_SHA = 'ede1614d8f303b725b0004ef3389fdc1e821a49941f169cf571169d39b79c989'
PATCH_INVENTORY_SHA = '95645004af11c61b204f2eda525ebbc85d86674c88a2b5807999cd5c8555a779'
HERE = Path(__file__).resolve().parent


def sha(data): return hashlib.sha256(data).hexdigest()


def safe_path(root, name):
    part = PurePosixPath(name)
    if part.is_absolute() or not part.parts or any(p in ('..','.') or ':' in p or '\\' in p for p in part.parts):
        raise ValueError('Unsafe payload path')
    path = root.joinpath(*part.parts)
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError('Payload or source path leaves candidate')
    return path


def verify_inventory(root, expected_sha):
    raw = (root/'SOURCE_INVENTORY.json').read_bytes()
    if sha(raw) != expected_sha:
        raise ValueError('Candidate inventory differs; no overwrite permitted')
    value = json.loads(raw)
    for name, expected in value['files'].items():
        path = safe_path(root,name)
        if not path.is_file() or sha(path.read_bytes()) != expected:
            raise ValueError('Candidate source differs: '+name)
    return value


def changed_payload():
    text = (HERE/'auth_feedback_payload.b64').read_text(encoding='ascii')
    raw = base64.b64decode(''.join(text.split()),validate=True)
    if sha(raw) != PAYLOAD_SHA: raise ValueError('Delivery checksum mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)): raise ValueError('Duplicate payload entry')
        files = {n:archive.read(n) for n in names}
    if sha(files['SOURCE_INVENTORY.json']) != PATCH_INVENTORY_SHA:
        raise ValueError('Updated inventory checksum mismatch')
    return files


def ensure_idle(code_root):
    if os.name != 'nt': return
    # Read process command lines, but never print their contents.
    query = ("$ErrorActionPreference='Stop'; @(Get-CimInstance Win32_Process | "
             "Where-Object { $_.Name -in @('python.exe','pythonw.exe') } | "
             "Select-Object ProcessId,CommandLine) | ConvertTo-Json -Compress")
    done = subprocess.run(['powershell','-NoProfile','-Command',query],
                          capture_output=True,text=True,timeout=20)
    if done.returncode: raise RuntimeError('Could not check active Python processes; no code changed')
    rows = json.loads(done.stdout or '[]')
    if isinstance(rows,dict): rows=[rows]
    needle = str(code_root).casefold()
    for row in rows:
        line = str(row.get('CommandLine') or '').casefold()
        if needle in line and any(x in line for x in ('run_daily_session.py','probe_universe_quote_watch.py')):
            raise RuntimeError('Daily-flow process still active (PID '+str(row['ProcessId'])+'); no live-code update permitted')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--code-root',type=Path,required=True)
    parser.add_argument('--data-root',type=Path,required=True)
    parser.add_argument('--allow-test-auth-stub',action='store_true',help='Non-Windows local development only')
    args=parser.parse_args(argv)
    if os.name=='nt' and args.allow_test_auth_stub:
        raise ValueError('Windows must use the installed mb_tools import')
    code=args.code_root.expanduser().resolve();data=args.data_root.expanduser().resolve()
    if not code.is_dir() or not data.is_dir(): raise ValueError('Existing code and data folders required')
    current=sha((code/'SOURCE_INVENTORY.json').read_bytes())
    if current==PATCH_INVENTORY_SHA:
        verify_inventory(code,PATCH_INVENTORY_SHA)
        print('ALREADY INSTALLED: daily-flow-auth-feedback-20261109. No files changed.')
        return 0
    inventory=verify_inventory(code,BASE_INVENTORY_SHA)
    files=changed_payload()
    for name in files: safe_path(code,name)
    for name in files:
        if name not in inventory['files'] and name!='SOURCE_INVENTORY.json' and safe_path(code,name).exists():
            raise ValueError('New target file already exists; no overwrite: '+name)
    ensure_idle(code)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:6]
    evidence=code/'output'/('auth-update-'+stamp)
    with tempfile.TemporaryDirectory(prefix='dfa_') as directory, ExitStack() as locks:
        stage=Path(directory)/'candidate';stage.mkdir()
        for name in inventory['files']:
            dst=safe_path(stage,name);dst.parent.mkdir(parents=True,exist_ok=True)
            dst.write_bytes(safe_path(code,name).read_bytes())
        for name,content in files.items():
            dst=safe_path(stage,name);dst.parent.mkdir(parents=True,exist_ok=True);dst.write_bytes(content)
        verify_inventory(stage,PATCH_INVENTORY_SHA)
        # Hold the established session/channel locks while testing/installing.
        # These are OS locks only; no session/journal file is written.
        sys.path.insert(0,str(stage/'market_data/src'))
        from mb_market_data.acquisition_health import operation_lock
        for file in sorted((data/'output/daily_session').glob('*/plan.json')):
            plan=json.loads(file.read_text(encoding='utf-8'))
            journal=Path(plan['journal']).resolve()
            if not journal.is_relative_to(data/'output'):
                raise ValueError('Session journal outside selected data root')
            key=str(journal).casefold()
            locks.enter_context(operation_lock('daily-session:'+key))
            for channel in ('uni','focus'):
                locks.enter_context(operation_lock('daily-channel:'+key+':'+channel))
        print('STAGED: authorization correction. Running offline checks before installation.',flush=True)
        command=[sys.executable,'-u','-B',str(stage/'verify_offline.py')]
        if args.allow_test_auth_stub: command.append('--allow-test-auth-stub')
        env=dict(os.environ);env.pop('PYTHONPATH',None)
        completed=subprocess.run(command,cwd=stage,env=env,check=False)
        evidence.mkdir(parents=True,exist_ok=False)
        if (stage/'output').exists(): shutil.copytree(stage/'output',evidence/'verification')
        if completed.returncode:
            print('NOT INSTALLED: offline verification failed. Existing candidate unchanged.')
            print('Evidence:',evidence);return 1
        ensure_idle(code)
        verify_inventory(code,BASE_INVENTORY_SHA)  # refuse a concurrent source edit
        backup=evidence/'original';backup.mkdir()
        for name in files:
            path=safe_path(code,name)
            if path.exists():
                dst=safe_path(backup,name);dst.parent.mkdir(parents=True,exist_ok=True);dst.write_bytes(path.read_bytes())
        applied=[]
        try:
            for name,content in files.items():
                target=safe_path(code,name);target.parent.mkdir(parents=True,exist_ok=True)
                temp=target.with_name(target.name+'.auth-update-'+uuid4().hex[:6])
                try:
                    temp.write_bytes(content);os.replace(temp,target);applied.append(name)
                finally: temp.unlink(missing_ok=True)
            verify_inventory(code,PATCH_INVENTORY_SHA)
        except BaseException:
            for name in reversed(applied):
                target=safe_path(code,name);old=safe_path(backup,name)
                if old.is_file(): target.write_bytes(old.read_bytes())
                else: target.unlink(missing_ok=True)
            raise
        report={'result':'INSTALLED','implementation':'daily-flow-auth-feedback-20261109',
            'code_root':str(code),'data_root_unchanged':str(data),'backup':str(backup),
            'updated_files':list(files),'tests_run':200,'network_used':False,
            'credentials_accessed':False,'session_started':False,
            'missed_0925_snapshot_reconstructed':False}
        (evidence/'installation.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(report,indent=2))
        print('INSTALLED: daily-flow-auth-feedback-20261109')
        print('Existing daily.cmd path is unchanged. No daily session was started.')
    return 0


if __name__=='__main__':
    try: raise SystemExit(main())
    except Exception as error:
        print('UPDATE BLOCKED:',type(error).__name__,str(error),file=sys.stderr)
        raise SystemExit(2)
