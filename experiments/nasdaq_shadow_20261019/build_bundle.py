"""Unique, manifest-checked Windows delivery; exclude every runtime/provider file."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import uuid
from zipfile import ZipFile, ZIP_DEFLATED

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shadow import bootstrap, CONTRACT, ROOT, PIN_COMMIT


def build(destination, *, code_commit):
    bootstrap()
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    stem = 'nasdaq_shadow_' + datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
    target = destination / (stem + '.zip')
    with tempfile.TemporaryDirectory(prefix='nasdaq-shadow-build-') as temporary:
        package = Path(temporary) / stem
        package.mkdir()
        for name in ('src', 'coordinator_src'):
            shutil.copytree(ROOT / name, package / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (package / 'tests').mkdir()
        shutil.copyfile(ROOT / 'tests/test_nasdaq_shadow.py', package / 'tests/test_nasdaq_shadow.py')
        shutil.copyfile(ROOT / 'tests/test_nasdaq_feed_capture.py', package / 'tests/test_nasdaq_feed_capture.py')
        experiment = Path('experiments/nasdaq_shadow_20261019')
        (package / experiment).mkdir(parents=True)
        for name in ('shadow.py', 'feed_capture.py', 'worker.py', 'run_review.py', 'build_bundle.py', 'coordinator_pin.json', 'README.md'):
            shutil.copyfile(ROOT / experiment / name, package / experiment / name)
        for name in ('19_verify_nasdaq_shadow.cmd', '20_run_nasdaq_shadow.cmd', '21_stop_nasdaq_shadow.cmd', '22_run_extended_nasdaq_shadow.cmd', 'pyproject.toml'):
            body = (ROOT / name).read_bytes()
            if name.endswith('.cmd'):
                body = body.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
            (package / name).write_bytes(body)
        shutil.copyfile(ROOT / experiment / 'README.md', package / 'README.md')
        (package / 'DELIVERY.json').write_text(json.dumps({'contract': CONTRACT, 'code_commit': code_commit,
            'coordinator_commit': PIN_COMMIT, 'offline_default': True, 'production_hot': False}, indent=2) + '\n', encoding='utf-8')
        files = {path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in sorted(package.rglob('*')) if path.is_file()}
        (package / experiment / 'source_manifest.json').write_text(json.dumps({'contract': CONTRACT, 'files': files}, indent=2) + '\n', encoding='utf-8')
        with ZipFile(target, 'x', ZIP_DEFLATED) as archive:
            for path in sorted(package.rglob('*')):
                if path.is_file():
                    archive.write(path, path.relative_to(package.parent).as_posix())
    return {'zip': str(target), 'stem': stem, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
            'bytes': target.stat().st_size, 'files': len(files) + 1}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination', required=True)
    parser.add_argument('--code-commit', required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.destination, code_commit=args.code_commit), indent=2))
