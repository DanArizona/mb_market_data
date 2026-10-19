"""Operator entry point; fresh local outputs and an offline synthetic source."""
from __future__ import annotations
import json
from pathlib import Path
import sys
import traceback
import unittest
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
from combined_load import (CONTRACT, bootstrap, check_verification, deny_network, read_json, request_stop, run,
                           text, utc_now, write_json)


def verify_sources():
    manifest = read_json(HERE / 'source_manifest.json')
    if manifest.get('contract') != CONTRACT or not manifest.get('files'):
        raise ValueError('Review source inventory contract differs')
    import hashlib
    for relative, expected in manifest['files'].items():
        path = (ROOT / relative).resolve()
        if not path.is_relative_to(ROOT) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Pinned review source differs: ' + relative)


class Tee:
    def __init__(self, *streams): self.streams = streams
    def write(self, value):
        for stream in self.streams: stream.write(value); stream.flush()
    def flush(self):
        for stream in self.streams: stream.flush()


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ('verify', 'run', 'stop'):
        print('Use 09_verify_combined.cmd, 10_run_combined.cmd or 11_stop_combined.cmd.')
        return 2
    deny_network()
    try:
        if sys.argv[1] == 'stop':
            print(request_stop(), flush=True)
            return 0
        verify_sources()
        bootstrap()
        from mb_market_data.sampling_acquisition import owner_lock
        import mb_market_data.sampling_incremental as incremental
        if Path(incremental.__file__).resolve() != ROOT / 'src/mb_market_data/sampling_incremental.py':
            raise ValueError('Unexpected market-data source')
        (ROOT / 'runs').mkdir(exist_ok=True)
        with owner_lock(ROOT / 'runs/combined_owner.lock'):
            verify = sys.argv[1] == 'verify'
            if not verify:
                check_verification()
            folder = ROOT / 'runs/combined' / (utc_now().strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
            folder.parent.mkdir(exist_ok=True)
            if verify:
                (ROOT / 'runs/combined_verification.json').unlink(missing_ok=True)
                print('COMBINED VERIFICATION: focused offline tests, then 60 seconds of actual elapsed polling.', flush=True)
                suite = unittest.TestSuite(unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern=pattern)
                    for pattern in ('test_sampling_acquisition.py', 'test_sampling_incremental.py', 'test_combined_load.py'))
                with (ROOT / 'runs/combined_tests.txt').open('w', encoding='utf-8') as file:
                    if not unittest.TextTestRunner(stream=Tee(sys.stdout, file), verbosity=2).run(suite).wasSuccessful():
                        return 1
            else:
                print('COMBINED LOAD: 5 minutes plus startup and final audit/replay. Four separate processes.', flush=True)
            print('Synthetic quotes only; network disabled. Uni :00/:30, Focus :05/:20/:35/:50, Hot every 2 seconds.', flush=True)
            print('Output: ' + str(folder), flush=True)
            print('Ctrl+C stops this experiment; 11_stop_combined.cmd can also be run from another idle CMD.', flush=True)
            try:
                result = run(folder, 60 if verify else 300, on_status=lambda s: print(s, flush=True))
            except KeyboardInterrupt:
                result = {'result': 'STOPPED', 'reason': 'operator_interrupt'}
            except Exception as exc:
                result = {'result': 'FAILED', 'error_type': type(exc).__name__, 'detail': str(exc)}
                if folder.is_dir():
                    (folder / 'failure.txt').write_text(traceback.format_exc(), encoding='utf-8')
            if folder.is_dir():
                write_json(folder / 'result.json', result)
            if result['result'] == 'PASS' and verify:
                import hashlib
                write_json(ROOT / 'runs/combined_verification.json', {'result': 'PASS', 'report': str(folder / 'report.json'),
                    'source_manifest_sha256': hashlib.sha256((HERE / 'source_manifest.json').read_bytes()).hexdigest(),
                    'report_sha256': hashlib.sha256((folder / 'report.json').read_bytes()).hexdigest()})
            if (folder / 'summary.txt').is_file():
                print((folder / 'summary.txt').read_text(encoding='utf-8'), flush=True)
            print('Result: ' + result['result'] + '; reports: ' + str(folder), flush=True)
            print('Finished. This command returns to its prompt.', flush=True)
            return 0 if result['result'] in ('PASS', 'STOPPED') else 1
    except Exception as exc:
        print('Combined review FAILED before launch: ' + str(exc), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
