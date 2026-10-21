"""Read saved XML from a finished failed shadow run; never import live code."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as XML


def syntax_report(raw):
    # Exactly the accepted nasdaq_halts.normalize_nasdaq_xml parsing copy.
    decoded = raw.decode('utf-8-sig', errors='replace')
    normalized = re.sub(r'(<\/?)ndaq:', r'\1', decoded)
    report = {
        'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
        'prefix': decoded[:160], 'suffix': decoded[-160:],
        'named_entity_counts': dict(Counter(re.findall(r'&[A-Za-z][A-Za-z0-9]*;', decoded)).most_common(20)),
    }
    try:
        raw.decode('utf-8-sig', errors='strict')
        report['strict_utf8'] = True
    except UnicodeDecodeError:
        report['strict_utf8'] = False
    try:
        root = XML.fromstring(normalized)
        report.update(parse='OK', root_tag=root.tag,
                      rss_channel=root.tag == 'rss' and root.find('channel') is not None,
                      item_count=len(root.findall('./channel/item')))
    except XML.ParseError as exc:
        line, column = exc.position
        lines = normalized.split('\n')
        current = lines[line - 1] if 0 < line <= len(lines) else ''
        report.update(parse='ParseError', message=str(exc), line=line, column=column,
                      context=current[max(0, column - 80):column + 80])
    return report


def bounded_bytes(path, limit):
    with path.open('rb') as handle:
        value = handle.read(limit + 1)
    if len(value) > limit:
        raise ValueError('Evidence file exceeds diagnostic size limit: ' + path.name)
    return value


def contained_file(path, folder):
    resolved = path.resolve()
    if not resolved.is_relative_to(folder) or not resolved.is_file():
        raise ValueError('Expected a regular evidence file inside this run')
    return resolved


def diagnose(folder):
    folder = Path(folder).resolve(strict=True)
    if not folder.is_dir():
        raise ValueError('Expected the exact failed run directory')
    result = json.loads(bounded_bytes(contained_file(folder / 'result.json', folder), 65536))
    if result.get('result') != 'FAILED' or result.get('error_type') != 'ParseError':
        raise ValueError('This diagnostic requires a finished FAILED ParseError run')
    contained_file(folder / 'STOP', folder)
    paths = sorted((folder / 'captures').glob('capture-*/raw.xml'))
    if not 0 < len(paths) <= 450:
        raise ValueError('Expected 1 to 450 retained raw captures')
    captures = []
    for path in paths:
        raw = bounded_bytes(contained_file(path, folder), 4_000_000)
        captures.append(dict(capture=path.parent.name,
                             saved_snapshot=(path.parent / 'snapshot.json').is_file(),
                             **syntax_report(raw)))
    logs = {}
    for purpose in ('resolution', 'hot'):
        path = folder / (purpose + '_calls.jsonl')
        if not path.exists():
            logs[purpose] = {'file_present': False, 'records': 0}
        else:
            data = bounded_bytes(contained_file(path, folder), 1_000_000)
            rows = [json.loads(line) for line in data.splitlines() if line.strip()]
            logs[purpose] = {'file_present': True, 'records': len(rows),
                             'dispatched_records': sum(row.get('dispatched') is True for row in rows)}
    return {'diagnostic': 'SAVED_CAPTURE_ONLY', 'run': folder.name,
            'result': 'FAILED', 'error_type': 'ParseError', 'captures': captures,
            'quote_call_logs': logs,
            'scope': 'Read-only saved bytes; no network, credentials, workers, SQLite access or rerun. '
                     'Call-log counts are retained records, not a complete provider-call audit.'}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print('Usage: python -I -B 22_diagnose_nasdaq_capture.py EXACT_FAILED_RUN_DIRECTORY')
        return 2
    try:
        # ASCII escaping prevents captured markup/control characters affecting the terminal.
        print(json.dumps(diagnose(argv[0]), indent=2, ensure_ascii=True))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({'diagnostic': 'UNAVAILABLE', 'error_type': type(exc).__name__,
                          'message': str(exc)}, ensure_ascii=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
