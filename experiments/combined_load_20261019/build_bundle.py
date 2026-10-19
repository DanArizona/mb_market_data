"""Build a uniquely named synthetic review ZIP from explicit pinned sources."""
from __future__ import annotations
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
from combined_load import CONTRACT, ROOT, verify_coordinator


def build(destination, *, code_commit):
    verify_coordinator()
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    stem = 'combined_synthetic_' + datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
    target = destination / (stem + '.zip')
    with tempfile.TemporaryDirectory(prefix='combined-bundle-build-') as temporary:
        package = Path(temporary) / stem
        package.mkdir()
        for name in ('src', 'coordinator_src'):
            shutil.copytree(ROOT / name, package / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (package / 'tests').mkdir()
        for name in ('test_sampling_acquisition.py', 'test_sampling_incremental.py', 'test_combined_load.py'):
            shutil.copyfile(ROOT / 'tests' / name, package / 'tests' / name)
        experiment = Path('experiments/combined_load_20261019')
        (package / experiment).mkdir(parents=True)
        for name in ('combined_load.py', 'worker.py', 'run_review.py', 'build_bundle.py', 'coordinator_pin.json', 'README.md'):
            shutil.copyfile(ROOT / experiment / name, package / experiment / name)
        for name in ('09_verify_combined.cmd', '10_run_combined.cmd', '11_stop_combined.cmd', 'pyproject.toml'):
            body = (ROOT / name).read_bytes()
            if name.endswith('.cmd'):
                body = body.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
            (package / name).write_bytes(body)
        shutil.copyfile(ROOT / experiment / 'README.md', package / 'README.md')
        (package / 'DELIVERY.json').write_text(json.dumps({'contract': CONTRACT, 'code_commit': code_commit,
            'coordinator_commit': '0417b345e2725f4e1e184d3b3202bedb846dbfca', 'synthetic_only': True,
            'created_at_utc': datetime.now(timezone.utc).isoformat()}, indent=2) + '\n', encoding='utf-8')
        files = {p.relative_to(package).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(package.rglob('*')) if p.is_file()}
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
