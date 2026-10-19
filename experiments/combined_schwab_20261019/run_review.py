"""Separate offline verification, authenticated bounded run, and owned stop."""
from pathlib import Path
import getpass
import hashlib
import json
import os
import sys
import unittest
import uuid

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
sys.path.insert(0,str(HERE))
from provider_load import (CONTRACT,SecureHorizon,bootstrap,check_live_window,check_verification,
    deny_network,load_roster,read_json,request_stop,run,utc_now,write_json)


def verify_sources():
    manifest=read_json(HERE/'source_manifest.json')
    if manifest.get('contract')!=CONTRACT or not manifest.get('files'):
        raise ValueError('Review inventory differs')
    for relative,expected in manifest['files'].items():
        path=(ROOT/relative).resolve()
        if not path.is_relative_to(ROOT) or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            raise ValueError('Pinned review source differs')


class Tee:
    def __init__(self,*streams): self.streams=streams
    def write(self,value):
        for stream in self.streams: stream.write(value);stream.flush()
    def flush(self):
        for stream in self.streams: stream.flush()


def resolve_ecfg():
    explicit=os.environ.get('MB_SCHWAB_ECFG')
    if explicit:
        path=Path(explicit).expanduser()
    else:
        vault=os.environ.get('MB_VAULT')
        path=(Path(vault).expanduser() if vault else Path.home()/'MBV')/'secure_schwabdev.ecfg'
    if path.suffix.lower()!='.ecfg' or not path.is_file():
        raise ValueError('Existing encrypted Schwab configuration required')
    return path.resolve()


def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ('verify','schwab','stop') or len(argv)!=(3 if argv[0]=='schwab' else 1):
        print('Use 12_verify_combined_schwab.cmd, 13_run_combined_schwab.cmd OPENING_R0 FOCUS_R1, or 14_stop_combined_schwab.cmd.')
        return 2
    credential=None
    folder=None
    try:
        if argv[0]=='stop':
            print(request_stop(),flush=True)
            return 0
        verify_sources(); bootstrap()
        from mb_market_data.sampling_acquisition import owner_lock
        from mb_market_data.hot_provider_trial import prepare_schwab_client
        (ROOT/'runs').mkdir(exist_ok=True)
        with owner_lock(ROOT/'runs/combined_provider_owner.lock'):
            offline=argv[0]=='verify'
            roster=None
            if offline:
                deny_network()
                (ROOT/'runs/combined_provider_verification.json').unlink(missing_ok=True)
                print('OFFLINE VERIFICATION: tests, then 60 seconds of actual-elapsed synthetic polling; no API/password.',flush=True)
                suite=unittest.TestSuite(unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern=p) for p in
                    ('test_sampling_acquisition.py','test_sampling_incremental.py','test_hot_provider_trial.py','test_combined_provider.py'))
                with (ROOT/'runs/provider_tests.txt').open('w',encoding='utf-8') as f:
                    if not unittest.TextTestRunner(stream=Tee(sys.stdout,f),verbosity=2).run(suite).wasSuccessful(): return 1
            else:
                check_verification()
                check_live_window(utc_now())
                roster=load_roster(argv[1],argv[2])
                ecfg=resolve_ecfg()
                print('COMBINED SCHWAB: 5 minutes after worker setup and minute alignment.',flush=True)
                print('Uni='+str(len(roster['opening_uni']))+'; Focus=40; initial Hot=40; outside-Uni probe=MSFT.',flush=True)
                print('Hard quote-call budgets: Uni 20 + Focus 20 + Hot 150 = 190; request timeout 3s.',flush=True)
                print('Uni :00/:30; Focus :05/:20/:35/:50; Hot every 2s. One password prompt.',flush=True)
                print('Proposal files are read-only inputs. Fresh experimental journals; normal production continues.',flush=True)
                print('SDK token refresh may update the shared token database; authentication HTTP calls are outside the quote-call budget.',flush=True)
                password=getpass.getpass('Encrypted config password: ')
                client=None
                try:
                    client,_=prepare_schwab_client(ecfg,password,SecureHorizon())
                    print('Encrypted configuration and credential horizon accepted.',flush=True)
                    credential={'ecfg':str(ecfg),'password':password}
                finally:
                    password=None
                    if client is not None: client.close()
            folder=ROOT/'runs/combined_provider'/(utc_now().strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8])
            folder.parent.mkdir(exist_ok=True)
            print('Output: '+str(folder),flush=True)
            print('Progress every 10 seconds. Ctrl+C or 14_stop_combined_schwab.cmd stops only this experiment.',flush=True)
            result=run(folder,60 if offline else 300,on_status=lambda v:print(v,flush=True),
                       source='synthetic_only' if offline else 'schwab',roster=roster,credential=credential)
            credential=None
            write_json(folder/'result.json',result)
            if offline and result['result']=='PASS':
                write_json(ROOT/'runs/combined_provider_verification.json',{'result':'PASS','report':str(folder/'report.json'),
                    'source_manifest_sha256':hashlib.sha256((HERE/'source_manifest.json').read_bytes()).hexdigest(),
                    'report_sha256':hashlib.sha256((folder/'report.json').read_bytes()).hexdigest()})
            if (folder/'summary.txt').is_file(): print((folder/'summary.txt').read_text(encoding='utf-8'),flush=True)
            print('Result: '+result['result']+'; reports: '+str(folder),flush=True)
            print('Finished. This command returns to its prompt.',flush=True)
            return 0 if result['result'] in ('PASS','STOPPED') else 1
    except KeyboardInterrupt:
        if folder is not None and folder.is_dir():write_json(folder/'result.json',{'result':'STOPPED','reason':'operator_interrupt'})
        print('Experiment interrupted; owned workers stopped. Inspect retained evidence for unresolved outcomes.',flush=True)
        return 130
    except Exception as exc:
        # Do not echo encrypted config, SDK exception strings, or authorization URLs.
        if folder is not None and folder.is_dir():write_json(folder/'result.json',{'result':'FAILED','error_type':type(exc).__name__})
        print('Combined provider review FAILED ('+type(exc).__name__+'). No further experiment requests.',flush=True)
        return 1
    finally:
        if credential: credential.clear()


if __name__=='__main__': raise SystemExit(main())
