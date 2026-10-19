"""Build a unique bounded-provider review archive from explicit generic sources."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import uuid
from zipfile import ZipFile,ZIP_DEFLATED

sys.path.insert(0,str(Path(__file__).resolve().parent))
from provider_load import CONTRACT,ROOT,verify_coordinator


def build(destination,*,code_commit):
    verify_coordinator()
    destination=Path(destination).resolve();destination.mkdir(parents=True,exist_ok=True)
    stem='combined_schwab_'+datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]
    target=destination/(stem+'.zip')
    with tempfile.TemporaryDirectory(prefix='combined-provider-build-') as temp:
        package=Path(temp)/stem;package.mkdir()
        for name in ('src','coordinator_src'):
            shutil.copytree(ROOT/name,package/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        (package/'tests').mkdir();(package/'probes').mkdir()
        for name in ('test_sampling_acquisition.py','test_sampling_incremental.py','test_hot_provider_trial.py','test_combined_provider.py'):
            shutil.copyfile(ROOT/'tests'/name,package/'tests'/name)
        shutil.copyfile(ROOT/'probes/probe_hot_quote_trial.py',package/'probes/probe_hot_quote_trial.py')
        experiment=Path('experiments/combined_schwab_20261019');(package/experiment).mkdir(parents=True)
        for name in ('provider_load.py','worker.py','run_review.py','build_bundle.py','coordinator_pin.json','README.md'):
            shutil.copyfile(ROOT/experiment/name,package/experiment/name)
        for name in ('12_verify_combined_schwab.cmd','13_run_combined_schwab.cmd','14_stop_combined_schwab.cmd','pyproject.toml'):
            body=(ROOT/name).read_bytes()
            if name.endswith('.cmd'):body=body.replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')
            (package/name).write_bytes(body)
        shutil.copyfile(ROOT/experiment/'README.md',package/'README.md')
        (package/'DELIVERY.json').write_text(json.dumps({'contract':CONTRACT,'code_commit':code_commit,
            'coordinator_commit':'0417b345e2725f4e1e184d3b3202bedb846dbfca',
            'live_requires_explicit_command':True,'created_at_utc':datetime.now(timezone.utc).isoformat()},indent=2)+'\n')
        files={v.relative_to(package).as_posix():hashlib.sha256(v.read_bytes()).hexdigest() for v in sorted(package.rglob('*')) if v.is_file()}
        (package/experiment/'source_manifest.json').write_text(json.dumps({'contract':CONTRACT,'files':files},indent=2)+'\n')
        with ZipFile(target,'x',ZIP_DEFLATED) as archive:
            for v in sorted(package.rglob('*')):
                if v.is_file():archive.write(v,v.relative_to(package.parent).as_posix())
    return {'zip':str(target),'stem':stem,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'bytes':target.stat().st_size,'files':len(files)+1}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--destination',required=True);parser.add_argument('--code-commit',required=True)
    args=parser.parse_args();print(json.dumps(build(args.destination,code_commit=args.code_commit),indent=2))
