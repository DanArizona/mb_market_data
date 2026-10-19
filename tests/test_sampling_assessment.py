import copy
import unittest
from mb_market_data.sampling_assessment import assess, assess_legacy_report, LEGACY_AVAILABILITY_FAILURE


class AssessmentTests(unittest.TestCase):
    def test_invalid_is_warning_but_collection_failure_is_not_hidden(self):
        result = assess(collection_failures=[], status_counts={'uni': {'quote': 5537, 'invalid': 10}})
        self.assertEqual(result['result'], 'PASS_WITH_WARNINGS')
        self.assertEqual(result['availability']['invalid_observations'], 10)
        self.assertFalse(result['all_observations_are_quotes'])
        self.assertEqual(assess(collection_failures=['missing slot'], status_counts={'uni': {'invalid': 10}})['result'], 'FAILED')

    def test_transport_missing_and_unknown_are_failures(self):
        for status in ('request_error', 'missing', 'unknown'):
            self.assertEqual(assess(collection_failures=[], status_counts={'hot': {status: 1}})['result'], 'FAILED')

    def test_bad_counts_and_empty_results_are_refused(self):
        for counts in ({}, {'uni': {}}, {'hot': {'quote': True}}, {'hot': {'quote': -1}}, {'other': {'quote': 1}}):
            with self.subTest(counts=counts), self.assertRaises(ValueError):
                assess(collection_failures=[], status_counts=counts)

    def original(self):
        channel = dict(expected_slots=10, committed_slots=10, missing_slots=0, complete=True,
                       no_full_scan_during_polling=True, status_counts={'quote': 20})
        report = dict(contract='isolated-combined-provider-v1', result='FAILED', failures=[LEGACY_AVAILABILITY_FAILURE],
            audit_passed=True, replays_identical=True, request_replay_identical=True, publication_replay_identical=True,
            channels={c: copy.deepcopy(channel) for c in ('uni', 'focus', 'hot')})
        report['channels']['uni']['status_counts']['invalid'] = 10
        return report

    def test_legacy_result_is_preserved_without_mutating_input(self):
        report = self.original(); before = copy.deepcopy(report)
        result = assess_legacy_report(report)
        self.assertEqual(result['result'], 'PASS_WITH_WARNINGS')
        self.assertEqual(result['original_result'], 'FAILED')
        self.assertEqual(report, before)

    def test_unrelated_original_failure_is_retained(self):
        report = self.original(); report['failures'].append('Unexplained failure')
        self.assertEqual(assess_legacy_report(report)['result'], 'FAILED')

    def test_partial_or_inconsistent_report_cannot_become_pass(self):
        for mutation in (lambda r: r.pop('audit_passed'), lambda r: r['channels']['hot'].update(missing_slots=1),
                         lambda r: r.update(result='PASS'), lambda r: r.update(failures=[]),
                         lambda r: r['channels'].pop('hot')):
            report = self.original(); mutation(report)
            with self.assertRaises(ValueError): assess_legacy_report(report)
