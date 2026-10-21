import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import socket


SOURCE = Path(__file__).resolve().parents[1] / 'experiments/nasdaq_shadow_20261019/22_diagnose_nasdaq_capture.py'
spec = importlib.util.spec_from_file_location('capture_diagnostic', SOURCE)
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)


class CaptureDiagnosticTests(unittest.TestCase):
    def test_accepted_unbound_ndaq_prefix_is_still_supported(self):
        report = diagnostic.syntax_report(b'<rss><channel><item><ndaq:IssueSymbol>ABC</ndaq:IssueSymbol></item></channel></rss>')
        self.assertEqual(report['parse'], 'OK')
        self.assertTrue(report['rss_channel'])
        self.assertEqual(report['item_count'], 1)

    def test_unknown_entity_position_and_context_are_retained(self):
        report = diagnostic.syntax_report(b'<rss>\n<channel><title>A&nbsp;B</title></channel></rss>')
        self.assertEqual(report['parse'], 'ParseError')
        self.assertEqual(report['line'], 2)
        self.assertIn('&nbsp;', report['context'])
        self.assertEqual(report['named_entity_counts'], {'&nbsp;': 1})

    def test_html_and_truncated_payloads_are_not_declared_valid_rss(self):
        html = diagnostic.syntax_report(b'<html><body>message</body></html>')
        self.assertEqual(html['parse'], 'OK')
        self.assertFalse(html['rss_channel'])
        self.assertEqual(diagnostic.syntax_report(b'<rss><channel>')['parse'], 'ParseError')

    def test_saved_diagnosis_has_no_network_or_file_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'captures/capture-one').mkdir(parents=True)
            (folder / 'result.json').write_text(json.dumps({'result': 'FAILED', 'error_type': 'ParseError'}))
            (folder / 'STOP').write_text('stopped')
            (folder / 'captures/capture-one/raw.xml').write_bytes(b'<rss><channel>')
            before = {p.relative_to(folder): (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.rglob('*') if p.is_file()}
            with patch.object(socket, 'socket', side_effect=AssertionError('Network forbidden')):
                report = diagnostic.diagnose(folder)
            after = {p.relative_to(folder): (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.rglob('*') if p.is_file()}
            self.assertEqual(before, after)
            self.assertFalse(report['captures'][0]['saved_snapshot'])
            self.assertFalse(report['quote_call_logs']['hot']['file_present'])

    def test_running_or_different_failure_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for result in ({'result': 'PASS'}, {'result': 'FAILED', 'error_type': 'TimeoutError'}):
                (folder / 'result.json').write_text(json.dumps(result))
                with self.assertRaisesRegex(ValueError, 'finished FAILED ParseError'):
                    diagnostic.diagnose(folder)

    def test_outside_capture_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            folder = parent / 'run'
            (folder / 'captures/capture-one').mkdir(parents=True)
            (folder / 'result.json').write_text(json.dumps({'result': 'FAILED', 'error_type': 'ParseError'}))
            (folder / 'STOP').write_text('stopped')
            (parent / 'outside.xml').write_bytes(b'<rss/>')
            (folder / 'captures/capture-one/raw.xml').symlink_to(parent / 'outside.xml')
            with self.assertRaisesRegex(ValueError, 'inside this run'):
                diagnostic.diagnose(folder)


if __name__ == '__main__':
    unittest.main()
