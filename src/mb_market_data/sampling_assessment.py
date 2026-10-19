"""Independent acquisition, transport and symbol-availability assessments.

An explicit provider-invalid result is a recorded observation. It is never a
replacement for a missing acquisition, and does not establish why a symbol was
unavailable. Callers must supply their independently verified collection checks.
"""
from collections import Counter

LEGACY_AVAILABILITY_FAILURE = 'Acquisitions contain unavailable quote outcomes; inspect status counts'


def assess(*, collection_failures, status_counts):
    if not isinstance(collection_failures, (list, tuple)) or any(not isinstance(v, str) or not v for v in collection_failures):
        raise ValueError('Explicit collection findings required')
    if not isinstance(status_counts, dict) or not status_counts:
        raise ValueError('Nonempty channel status counts required')
    totals = Counter()
    for channel, counts in status_counts.items():
        if channel not in ('uni', 'focus', 'hot') or not isinstance(counts, dict):
            raise ValueError('Unsupported channel/counts')
        for status, count in counts.items():
            if not isinstance(status, str) or type(count) is not int or count < 0:
                raise ValueError('Invalid observation status count')
            totals[status] += count
    if not sum(totals.values()):
        raise ValueError('No observations to assess')
    unknown = {k: v for k, v in totals.items() if v and k not in ('quote', 'invalid', 'missing', 'request_error')}
    transport_failed = totals['request_error'] > 0
    availability_failed = totals['missing'] > 0 or bool(unknown)
    warned = totals['invalid'] > 0
    result = 'FAILED' if collection_failures or transport_failed or availability_failed else ('PASS_WITH_WARNINGS' if warned else 'PASS')
    return {'result': result,
        'collection_integrity': {'result': 'FAILED' if collection_failures else 'PASS', 'findings': list(collection_failures)},
        'transport': {'result': 'FAILED' if transport_failed else 'PASS', 'request_error_observations': totals['request_error']},
        'availability': {'result': 'FAILED' if availability_failed else ('WARNING' if warned else 'PASS'),
            'invalid_observations': totals['invalid'], 'missing_observations': totals['missing'], 'unknown_statuses': unknown},
        'all_observations_are_quotes': not any(v for k, v in totals.items() if k != 'quote'),
        'status_counts': dict(sorted(totals.items()))}


def assess_legacy_report(report):
    """Reclassify only a complete supported report; never edit its old verdict."""
    if report.get('contract') != 'isolated-combined-provider-v1' or report.get('result') not in ('PASS', 'FAILED'):
        raise ValueError('Unsupported original report')
    failures = report.get('failures')
    if not isinstance(failures, list) or any(not isinstance(f, str) or not f for f in failures):
        raise ValueError('Malformed original findings')
    if (report['result'] == 'PASS') != (not failures):
        raise ValueError('Original verdict/findings disagree')
    checks = ('audit_passed', 'replays_identical', 'request_replay_identical', 'publication_replay_identical')
    if any(report.get(k) is not True for k in checks):
        raise ValueError('Independent original checks must all pass')
    if set(report.get('channels', {})) != {'uni', 'focus', 'hot'}:
        raise ValueError('Three complete channel reports required')
    counts = {}
    for channel, value in report['channels'].items():
        expected, committed = value.get('expected_slots'), value.get('committed_slots')
        if type(expected) is not int or expected <= 0 or type(committed) is not int:
            raise ValueError('Invalid slot counts')
        if expected != committed or value.get('missing_slots') != 0 or value.get('complete') is not True or value.get('no_full_scan_during_polling') is not True:
            raise ValueError('Original collection coverage not complete')
        counts[channel] = value.get('status_counts')
    collection = [f for f in failures if f != LEGACY_AVAILABILITY_FAILURE]
    result = assess(collection_failures=collection, status_counts=counts)
    had_nonquote = not result['all_observations_are_quotes']
    if (LEGACY_AVAILABILITY_FAILURE in failures) != had_nonquote:
        raise ValueError('Original availability finding disagrees with counts')
    return dict(result, original_result=report['result'], original_failures=list(failures))
