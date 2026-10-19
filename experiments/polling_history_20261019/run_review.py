"""Offline verification, full history proof, read-only assessment, owned stop."""
import hashlib
from pathlib import Path
import sys
import unittest
import uuid

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from history_proof import CONTRACT,ROOT,bootstrap,deny_network,load_config,read_json,require,run,utc_now,write_json


def verify_sources():
    value=read_json(HERE/'source_manifest.json')
    require(value.get('contract')==CONTRACT and bool(value.get('files')),'Inventory differs')
    for relative,expected in value['files'].items():
        path=(ROOT/relative).resolve()
        require(path.is_relative_to(ROOT) and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==expected,'Pinned source differs: '+relative)


def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    folder=None
    try:
        require(argv and argv[0] in ('verify','full','assess','stop') and len(argv)==(2 if argv[0]=='assess' else 1),'Use the supplied 15/16/17/18 commands')
        deny_network();verify_sources();bootstrap()
        runs=ROOT/'runs';runs.mkdir(exist_ok=True)
        pointer=runs/'active_history.json'
        if argv[0]=='stop':
            require(pointer.is_file(),'No history experiment has started in this package')
            value=read_json(pointer);folder=Path(value['folder']).resolve()
            require(value['contract']==CONTRACT and folder.parent==(runs/'history').resolve(),'Active pointer differs')
            load_config(folder)
            if (folder/'result.json').exists():print('Experiment already finished: '+read_json(folder/'result.json')['result']);return 0
            (folder/'STOP').write_text('operator requested stop\n',encoding='utf-8')
            print('Stop requested for this offline experiment only.');return 0
        stamp=utc_now().strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
        if argv[0]=='assess':
            from reassess import reassess
            value=reassess(argv[1],runs/'assessments'/stamp,on_status=lambda v:print(v,flush=True))
            print('Derived report: '+str(runs/'assessments'/stamp),flush=True)
            return 0 if value['result'] in ('PASS','PASS_WITH_WARNINGS') else 1
        from mb_market_data.sampling_acquisition import owner_lock
        with owner_lock(runs/'history_owner.lock'):
            if argv[0]=='verify':
                print('OFFLINE VERIFICATION: focused tests, then 3 synthetic session minutes; final minute uses actual elapsed scheduling.',flush=True)
                suite=unittest.TestSuite(unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern=p) for p in
                    ('test_sampling_assessment.py','test_polling_history.py','test_sampling_acquisition.py','test_sampling_incremental.py'))
                if not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful():return 1
            else:
                gate=read_json(runs/'history_verification.json')
                require(gate.get('result')=='PASS' and gate.get('manifest_sha256')==hashlib.sha256((HERE/'source_manifest.json').read_bytes()).hexdigest(), 'Run 15_verify_history.cmd on this unchanged package first')
                report=Path(gate['report']).resolve()
                require(report.parent.parent==(runs/'history').resolve() and hashlib.sha256(report.read_bytes()).hexdigest()==gate['report_sha256'],'Verification receipt changed')
                print('FULL OFFLINE HISTORY: logical native prefill through 15:58:58; final session minute at actual elapsed speed.',flush=True)
                print('554 Uni / 40 Focus / 40 Hot; approximately 962,520 regular-session observations plus lifecycle changes.',flush=True)
                print('Needs at least 6 GiB free disk. Cold reconstruction and repeated recovery can take several minutes.',flush=True)
            (runs/'history').mkdir(exist_ok=True)
            folder=runs/'history'/stamp
            write_json(pointer,dict(contract=CONTRACT,folder=str(folder)))
            print('Output: '+str(folder),flush=True)
            print('No API, password, source install or production journal. Ctrl+C or 17_stop_history.cmd stops only this experiment.',flush=True)
            value=run(folder,'verify' if argv[0]=='verify' else 'full',on_status=lambda v:print(v,flush=True))
            write_json(folder/'result.json',value)
            if argv[0]=='verify' and value['result']=='PASS':
                write_json(runs/'history_verification.json',dict(result='PASS',report=str(folder/'report.json'),
                    manifest_sha256=hashlib.sha256((HERE/'source_manifest.json').read_bytes()).hexdigest(),report_sha256=hashlib.sha256((folder/'report.json').read_bytes()).hexdigest()))
            print('Finished. This command returns to its prompt.',flush=True)
            return 0 if value['result']=='PASS' else 1
    except (KeyboardInterrupt,InterruptedError):
        if folder and folder.is_dir():write_json(folder/'result.json',dict(result='STOPPED',reason='operator_stop'))
        print('Offline experiment stopped. Evidence retained; production pollers unchanged.',flush=True);return 130
    except Exception as exc:
        if folder and folder.is_dir() and argv[0]!='stop':write_json(folder/'result.json',dict(result='FAILED',error_type=type(exc).__name__,detail=str(exc)))
        import traceback
        traceback.print_exc()
        return 1


if __name__=='__main__':raise SystemExit(main())
