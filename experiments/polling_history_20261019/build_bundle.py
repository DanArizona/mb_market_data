"""Unique offline bundle; no provider files, credentials or runtime evidence."""
from datetime import datetime,timezone
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import uuid
from zipfile import ZipFile,ZIP_DEFLATED

sys.path.insert(0,str(Path(__file__).resolve().parent))
from history_proof import CONTRACT,ROOT
from provider_load import verify_coordinator


def build(destination,*,code_commit):
    verify_coordinator()
    destination=Path(destination).resolve();destination.mkdir(parents=True,exist_ok=True)
    stem='polling_history_'+datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]
    target=destination/(stem+'.zip')
    with tempfile.TemporaryDirectory(prefix='polling-history-build-') as temp:
        package=Path(temp)/stem;package.mkdir()
        for name in ('src','coordinator_src'):
            shutil.copytree(ROOT/name,package/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        (package/'tests').mkdir()
        for name in ('test_sampling_assessment.py','test_polling_history.py','test_sampling_acquisition.py','test_sampling_incremental.py'):
            shutil.copyfile(ROOT/'tests'/name,package/'tests'/name)
        old=Path('experiments/combined_schwab_20261019');(package/old).mkdir(parents=True)
        for name in ('provider_load.py','coordinator_pin.json'):
            shutil.copyfile(ROOT/old/name,package/old/name)
        experiment=Path('experiments/polling_history_20261019');(package/experiment).mkdir(parents=True)
        for name in ('history_proof.py','worker.py','run_review.py','reassess.py','build_bundle.py','README.md'):
            shutil.copyfile(ROOT/experiment/name,package/experiment/name)
        for name in ('15_verify_history.cmd','16_run_full_history.cmd','17_stop_history.cmd','18_assess_saved_combined.cmd','pyproject.toml'):
            body=(ROOT/name).read_bytes()
            if name.endswith('.cmd'):body=body.replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')
            (package/name).write_bytes(body)
        shutil.copyfile(ROOT/experiment/'README.md',package/'README.md')
        (package/'DELIVERY.json').write_text(json.dumps(dict(contract=CONTRACT,code_commit=code_commit,
            coordinator_commit='0417b345e2725f4e1e184d3b3202bedb846dbfca',network_disabled=True),indent=2)+'\n')
        files={v.relative_to(package).as_posix():hashlib.sha256(v.read_bytes()).hexdigest() for v in sorted(package.rglob('*')) if v.is_file()}
        (package/experiment/'source_manifest.json').write_text(json.dumps(dict(contract=CONTRACT,files=files),indent=2)+'\n')
        with ZipFile(target,'x',ZIP_DEFLATED) as archive:
            for v in sorted(package.rglob('*')):
                if v.is_file():archive.write(v,v.relative_to(package.parent).as_posix())
    return dict(zip=str(target),stem=stem,sha256=hashlib.sha256(target.read_bytes()).hexdigest(),bytes=target.stat().st_size,files=len(files)+1)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--destination',required=True);p.add_argument('--code-commit',required=True)
    args=p.parse_args();print(json.dumps(build(args.destination,code_commit=args.code_commit),indent=2))
