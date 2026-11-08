"""Real-time, no-provider comparison of the delivered heartbeat readers.

Only three pinned source files are read; runtime packages/auth are not imported.
The old decision and new guard receive the SAME first read in each check. New
retries perform real reads. No exceptions, locks, or clock jumps are injected.
This is a filesystem/control diagnostic, not a market-data collection session.
"""
from __future__ import annotations

import argparse
import ast
import csv
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import signal
import socket
import sys
import time
from uuid import uuid4

PINS = {
    'runtime': ('daily_runtime.py', '110eb75a0aa340da75c1846bab7ff43a005eefd43796da51c52e431d48ec9e11'),
    'writer': ('acquisition_health.py', '446f7a9f27082056c3ecb0c38409bd616405e679a6888ead0cd0ac00b7d8742a'),
    'guard': ('daily_worker_liveness.py', 'dc720c339ab01b730045240b50b42e4fa2cda87d40dae9f2d3718d084f7e2921'),
}


def utc_now():
    return datetime.now(timezone.utc)


def error_code(error):
    return dict(error_type=type(error).__name__, errno=getattr(error, 'errno', None),
                winerror=getattr(error, 'winerror', None))


def deny_network():
    def denied(*args, **kwargs):
        raise OSError('Network disabled in heartbeat diagnostic')
    socket.create_connection = socket.getaddrinfo = denied
    socket.socket.connect = socket.socket.connect_ex = denied


def source_text(root, kind):
    name, expected = PINS[kind]
    path = Path(root) / 'market_data' / 'src' / 'mb_market_data' / name
    text = path.read_text(encoding='utf-8')  # LF normalization tolerates CRLF copies.
    if hashlib.sha256(text.encode('utf-8')).hexdigest() != expected:
        raise ValueError(f'Unexpected source: {path}; no files changed')
    return text


def functions(text, names):
    tree = ast.parse(text)
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    if {n.name for n in nodes} != set(names):
        raise ValueError('Expected pinned function definitions absent')
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future] + nodes, type_ignores=[]))
    space = dict(Path=Path, datetime=datetime, json=json, time=time, os=os, now=utc_now)
    exec(compile(module, '<pinned-diagnostic-functions>', 'exec'), space)
    return space


def load_sources(base, corrected):
    old = functions(source_text(base, 'runtime'), {'read_json', 'owner_available'})
    writer = functions(source_text(base, 'writer'), {'atomic_text', 'atomic_json'})
    guard = {'__name__': 'isolated_heartbeat_guard'}
    exec(compile(source_text(corrected, 'guard'), '<pinned-heartbeat-guard>', 'exec'), guard)
    return old, writer['atomic_json'], guard['OwnerHeartbeatGuard']


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def reader_process(base, corrected, root_text, owner_pid, label, done):
    # Ctrl+C belongs to the foreground parent, which requests a clean stop.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    deny_network()
    root = Path(root_text)
    report = dict(reader=label, pid=os.getpid(), checks=0, physical_reads=0,
                  read_errors=0, old_unavailable_checks=0, first_old_failure=None,
                  corrected_stop=None, max_check_gap_seconds=0.0, error=None)
    try:
        old, _, guard_type = load_sources(base, corrected)
        raw_read = old['read_json']
        with (root / f'{label}-events.jsonl').open('x', encoding='utf-8') as events, \
             (root / f'{label}-checks.csv').open('x', encoding='utf-8', newline='') as checks:
            def emit(value):
                events.write(json.dumps({'at': utc_now().isoformat(), **value}, sort_keys=True) + '\n')
                events.flush()

            def physical(path):
                report['physical_reads'] += 1
                try:
                    return raw_read(path)
                except (OSError, ValueError, UnicodeError) as error:
                    report['read_errors'] += 1
                    emit(dict(event='physical_read_error', **error_code(error)))
                    raise

            first = {}
            def old_read(path):
                try:
                    first['receipt'] = physical(path)
                    return first['receipt']
                except (OSError, ValueError, UnicodeError) as error:
                    first['error'] = error
                    raise

            pending = [False]
            def corrected_read(path):
                if pending[0]:
                    pending[0] = False
                    if 'error' in first:
                        raise first['error']
                    return first['receipt']
                return physical(path)

            old['read_json'] = old_read
            guard = guard_type(root, pid=owner_pid, reader=corrected_read, clock=utc_now, emit=emit)
            out = csv.writer(checks)
            out.writerow(['at_utc', 'gap_seconds', 'old_available', 'corrected_available',
                          'heartbeat_at', 'first_read_error', 'errno', 'winerror',
                          'check_elapsed_seconds', 'corrected_reason'])
            previous = time.monotonic()
            flushed = previous
            while not done.is_set():
                started = time.monotonic()
                gap = started - previous
                previous = started
                report['max_check_gap_seconds'] = max(report['max_check_gap_seconds'], gap)
                first.clear()
                old_ok = bool(old['owner_available'](root, pid=owner_pid))
                pending[0] = True
                new_ok = guard.available()
                at = utc_now().isoformat()
                report['checks'] += 1
                receipt = first.get('receipt') or {}
                code = error_code(first['error']) if 'error' in first else {}
                detail = dict(at=at, heartbeat_at=receipt.get('at'), old_available=old_ok,
                              corrected_available=new_ok, corrected_reason=guard.reason, **code)
                if not old_ok:
                    report['old_unavailable_checks'] += 1
                    if report['first_old_failure'] is None:
                        report['first_old_failure'] = detail
                    emit(dict(event='old_would_stop', **detail))
                if not new_ok:
                    report['corrected_stop'] = detail
                out.writerow([at, round(gap, 6), old_ok, new_ok, receipt.get('at'),
                              code.get('error_type'), code.get('errno'), code.get('winerror'),
                              round(time.monotonic() - started, 6), guard.reason])
                if not old_ok or not new_ok or time.monotonic() - flushed >= 1.0:
                    checks.flush()
                    flushed = time.monotonic()
                if not new_ok:
                    break  # Retain the guard's real, latched stop; never reset it.
                done.wait(max(0.0, 0.25 - (time.monotonic() - started)))
    except Exception as error:
        report['error'] = error_code(error)
    finally:
        save(root / f'{label}-result.json', report)


def classify(reports, ended, process_errors):
    if process_errors or len(reports) != 2 or any(r.get('error') or not r.get('checks') for r in reports):
        return 'DIAGNOSTIC_INCOMPLETE'
    if any(r['corrected_stop'] for r in reports):
        return 'CORRECTED_STOP_OBSERVED'
    if any(r['old_unavailable_checks'] for r in reports):
        return 'OLD_STOP_CONDITION_OBSERVED_FIXED_CONTINUED'
    return 'NO_STOP_CONDITION_OBSERVED' if ended == 'duration_complete' else 'INTERRUPTED_NO_STOP_OBSERVED'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, default=Path.home() / 'dailyflow_qa/daily_flow_candidate_20261106')
    parser.add_argument('--corrected', type=Path, default=Path.home() / 'dailyflow_uptime_qa/daily_flow_uptime_20261106')
    parser.add_argument('--minutes', type=float, default=60.0)
    parser.add_argument('--output-root', type=Path, default=Path.home() / 'hbdiag')
    args = parser.parse_args(argv)
    if not 0 < args.minutes <= 240:
        parser.error('--minutes must be greater than zero and no greater than 240')
    base, corrected, output = args.base.resolve(), args.corrected.resolve(), args.output_root.resolve()
    if any(output == p or output.is_relative_to(p) for p in (base, corrected)):
        parser.error('Use a separate diagnostic output root, not a candidate folder')
    deny_network()
    _, writer, _ = load_sources(base, corrected)  # Validate before creating output.
    root = output / (utc_now().strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:6])
    root.mkdir(parents=True, exist_ok=False)
    manifest = dict(contract='real-time-heartbeat-comparison-v1', started_at_utc=utc_now().isoformat(),
                    platform=sys.platform, python=sys.version, source_pins=PINS,
                    duration_minutes=args.minutes, writer_interval_seconds=2.0,
                    reader_interval_seconds=0.25, reader_processes=2, clock='real UTC and monotonic',
                    same_first_read=True, deliberate_fault_injection=False,
                    original_live_shutdown_cause_proven=False, live_uptime_qualified=False,
                    real_provider_calls=False, credentials_accessed=False, production_changes=False,
                    source_reads_only=[str(base), str(corrected)], evidence=str(root))
    save(root / 'run.json', manifest)
    print('OFFLINE HEARTBEAT DIAGNOSTIC: no broker, credentials, journals, or production writes.', flush=True)
    print(f'Evidence: {root}', flush=True)
    print('Two test readers; actual clock; heartbeat every 2s; reads every 0.25s. Ctrl+C saves partial results.', flush=True)
    context = mp.get_context('spawn')
    done = context.Event()
    children = []
    began = time.monotonic()
    last_write = began
    stats = dict(successful_writes=0, write_errors=0, max_write_gap_seconds=0.0, max_write_seconds=0.0)
    ended, process_errors = 'duration_complete', []
    events = (root / 'writer-events.jsonl').open('x', encoding='utf-8')

    def publish():
        nonlocal last_write
        start = time.monotonic()
        stats['max_write_gap_seconds'] = max(stats['max_write_gap_seconds'], start - last_write)
        last_write = start
        at = utc_now().isoformat()
        try:
            writer(root / 'owner.json', dict(pid=os.getpid(), at=at, active=True))
            stats['successful_writes'] += 1
        except (OSError, ValueError) as error:
            stats['write_errors'] += 1
            events.write(json.dumps(dict(event='writer_error', at=at, **error_code(error))) + '\n')
            events.flush()
        stats['max_write_seconds'] = max(stats['max_write_seconds'], time.monotonic() - start)

    try:
        publish()
        if not (root / 'owner.json').is_file():
            raise RuntimeError('Initial heartbeat publication failed')
        for label in ('reader1', 'reader2'):
            p = context.Process(target=reader_process, args=(str(base), str(corrected), str(root), os.getpid(), label, done))
            p.start()
            children.append(p)
        next_write, next_progress = began + 2.0, began + 60.0
        while time.monotonic() - began < args.minutes * 60:
            if any(not p.is_alive() for p in children):
                ended = 'reader_ended'
                break
            current = time.monotonic()
            if current >= next_write:
                publish()
                next_write = time.monotonic() + 2.0
            if current >= next_progress:
                print(f'Elapsed {int((current-began)/60)} minute(s); heartbeat writes {stats["successful_writes"]}; write errors {stats["write_errors"]}; both diagnostic readers alive.', flush=True)
                next_progress = current + 60.0
            done.wait(0.05)
    except KeyboardInterrupt:
        ended = 'operator_interrupted'
    except Exception as error:
        ended = 'diagnostic_error'
        process_errors.append(error_code(error))
    finally:
        done.set()
        for p in children:
            p.join(timeout=5)
            if p.is_alive():
                p.terminate(); p.join(timeout=5)
                process_errors.append(dict(pid=p.pid, error='owned_diagnostic_child_forced'))
            if p.exitcode not in (0, None):
                process_errors.append(dict(pid=p.pid, exitcode=p.exitcode))
        events.close()
        reports = []
        for label in ('reader1', 'reader2'):
            try:
                reports.append(json.loads((root / f'{label}-result.json').read_text(encoding='utf-8')))
            except (OSError, ValueError) as error:
                process_errors.append(dict(reader=label, **error_code(error)))
        result = dict(manifest, ended=ended, elapsed_seconds=round(time.monotonic()-began, 3),
                      completed_at_utc=utc_now().isoformat(), writer=stats, readers=reports,
                      process_errors=process_errors, result=classify(reports, ended, process_errors))
        save(root / 'result.json', result)
        print(json.dumps(result, indent=2), flush=True)
        print(f'RETAINED RESULT: {root / "result.json"}', flush=True)
    return 2 if result['result'] == 'DIAGNOSTIC_INCOMPLETE' else 0


if __name__ == '__main__':
    mp.freeze_support()
    raise SystemExit(main())
