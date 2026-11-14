"""Offline candidate checks: synthetic broker data, real acquisition/artifact code.

The accepted watchlists selector bridge is substituted locally; its installed
implementation is not bundled or reimplemented in this test fixture.
"""
from contextlib import redirect_stdout
from datetime import date, datetime, time, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import runpy
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from mb_market_data.opening_hierarchy import load_opening_proposal, write_opening_proposal
from mb_market_data.sampling_membership import SamplingHierarchyRevision
from mb_market_data.postclose_universe import build_with_client, handoff
from mb_market_data.volume_proxy_experiment import rank_values, run_comparison, volume_value, write_json
from test_nasdaq_symbol_directory import NASDAQ_RAW, OTHER_RAW

ET = ZoneInfo('America/New_York')
DAY = date(2026, 11, 5)
ROOT = Path(__file__).resolve().parents[1]


class Response:
    status_code = 200
    ok = True
    headers = {}
    def __init__(self, payload): self.payload = payload
    def json(self): return self.payload


class Clock:
    def __init__(self, hour=9, minute=0, second=0):
        self.now = datetime.combine(DAY, time(hour, minute, second), ET)
    def __call__(self): return self.now
    def sleep(self, seconds): self.now += timedelta(seconds=seconds)
    def datetime_type(self):
        owner = self
        class ClockDateTime(datetime):
            @classmethod
            def now(cls, tz=None): return owner.now.astimezone(tz or ET)
        return ClockDateTime


class Client:
    def __init__(self, clock, values=None, missing=(), bad_history=(), root=None):
        self.clock, self.values = clock, values or {}
        self.missing, self.bad_history = set(missing), set(bad_history)
        self.calls = []; self.closed = 0; self.root = root
    def quotes(self, symbols, *, fields):
        self.calls.append(('quotes', tuple(symbols), id(self)))
        regular = self.clock().replace(hour=16, minute=0)
        return Response({s: {'symbol': s, 'assetMainType': 'EQUITY',
                             'quote': {'totalVolume': self.values.get(s, 20000), 'lastPrice': 2.0},
                             'regular': {'regularMarketLastPrice': 2.0,
                                         'regularMarketTradeTime': int(regular.timestamp()*1000)},
                             'fundamental': {'sharesOutstanding': 10000000}}
                         for s in symbols if s not in self.missing})
    def price_history(self, symbol, **kwargs):
        if self.root is not None:
            assert (self.root / 'vol_test.json').is_file(), 'Snapshot must be saved before history starts'
        self.calls.append(('history', symbol, id(self)))
        if symbol in self.bad_history:
            return Response({'symbol': symbol, 'invalid': True})
        stamp = self.clock().replace(hour=8, minute=55, second=0)
        value = 5000 - int(symbol[1:]) if symbol.startswith('X') else 10000
        return Response({'symbol': symbol, 'candles': [
            {'datetime': int(stamp.timestamp()*1000), 'volume': value},
            {'datetime': int(stamp.replace(hour=9, minute=0).timestamp()*1000), 'volume': 999999}]})
    def close(self): self.closed += 1


def synthetic_focus_bridge(manifest, opening, output, limit, generated_at):
    """Test double at the unchanged external selector boundary, clearly labelled."""
    import csv
    values = {}
    with (manifest.parent / 'api_ov_observations.csv').open() as f:
        for row in csv.DictReader(f): values[row['symbol']] = int(row['ov_decision'])
    symbols = list(rank_values(values)[:limit])
    output.mkdir()
    r0 = load_opening_proposal(opening)
    r1 = SamplingHierarchyRevision(
        session_date=r0.session_date, revision=1, effective_at=r0.effective_at,
        uni_symbols=r0.uni_symbols, focus_symbols=tuple(symbols), hot_symbols=(),
        source='synthetic-reference-bridge', reason='offline test only')
    proposal = output / 'sampling_hierarchy_r1.json'
    write_json(proposal, {'session_date': str(r1.session_date), 'revision': 1,
                          'effective_at': r1.effective_at.isoformat(),
                          'uni_symbols': list(r1.uni_symbols), 'focus_symbols': list(r1.focus_symbols),
                          'hot_symbols': [], 'source': 'synthetic-reference-bridge'})
    return {'symbols': symbols, 'proposal': str(proposal), 'manifest': 'synthetic-test-only'}


class VolumeProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.output = self.root / 'comparison'
        self.clock = Clock(); self.opening = self.root / 'r0.json'
        self.opening_for(45)
    def opening_for(self, count):
        self.opening.unlink(missing_ok=True)
        revision = SamplingHierarchyRevision(
            session_date=DAY, revision=0, effective_at=datetime.combine(DAY, time(9,30), ET),
            uni_symbols=tuple(f'X{i:03}' for i in range(count)), focus_symbols=(), hot_symbols=(),
            source='synthetic-test', reason='offline fixture')
        write_opening_proposal(self.opening, revision)
    def exercise(self, client, **options):
        with patch('mb_market_data.schwab_quotes.datetime', self.clock.datetime_type()), redirect_stdout(StringIO()):
            return run_comparison(client, opening_path=self.opening, output_dir=self.output,
                                  snapshot_at=datetime.combine(DAY, time(9), ET),
                                  clock=self.clock, sleep=self.clock.sleep, request_interval=0,
                                  focus_builder=synthetic_focus_bridge, **options)
    def test_555_symbols_two_quote_calls_then_555_history_calls_same_client(self):
        self.opening_for(555)
        client = Client(self.clock, root=self.output)
        report = self.exercise(client)
        self.assertEqual(report['result'], 'COMPARISON_COMPLETE')
        self.assertEqual(report['quote_requests'], 2)
        self.assertEqual(report['history_requests'], 555)
        self.assertEqual([len(c[1]) for c in client.calls[:2]], [400, 155])
        self.assertEqual([c[0] for c in client.calls], ['quotes']*2 + ['history']*555)
        self.assertEqual({c[2] for c in client.calls}, {id(client)})
        self.assertEqual(client.closed, 0)
        self.assertFalse(report['membership_published'])
        self.assertEqual(report['common_count'], 40)
        self.assertEqual(json.loads(Path(report['reference_artifacts']['proposal']).read_text())['revision'], 1)
    def test_38_common_reports_two_swaps_not_volume_error(self):
        client = Client(self.clock, values={'X043': 100000, 'X044': 100001}, root=self.output)
        report = self.exercise(client)
        self.assertEqual(report['common_count'], 38)
        self.assertEqual(report['overlap_fraction_of_reference'], .95)
        self.assertEqual(report['only_vol_test'], ['X043', 'X044'])
        self.assertEqual(report['only_focus_r1'], ['X038', 'X039'])
    def test_missing_quote_does_not_stop_reference_or_become_zero(self):
        report = self.exercise(Client(self.clock, missing=['X000'], root=self.output))
        self.assertEqual(report['result'], 'COMPARISON_WITH_MISSING_PROXY_DATA')
        self.assertEqual(report['proxy_missing_symbols'], ['X000'])
        self.assertIn('X000', report['Focus_r1'])
        frozen = json.loads((self.output / 'vol_test.json').read_text())
        self.assertIsNone(next(x for x in frozen['rows'] if x['symbol']=='X000')['current_volume'])
    def test_invalid_volume_values_are_missing_zero_is_valid(self):
        for value in (None, '10', True, -1, 0.5, float('inf'), float('nan')):
            self.assertIsNone(volume_value(value))
        self.assertEqual(volume_value(0), 0); self.assertEqual(volume_value(12.0), 12)
        self.assertEqual(rank_values({'Z': 0, 'A': 0, 'B': None}), ('A','Z'))
    def test_quote_timeout_still_attempts_reference(self):
        client=Client(self.clock, root=self.output)
        client.quotes=Mock(side_effect=TimeoutError('synthetic'))
        report=self.exercise(client)
        self.assertEqual(report['result'], 'COMPARISON_WITH_MISSING_PROXY_DATA')
        self.assertEqual(len(report['proxy_missing_symbols']),45)
        self.assertEqual(report['history_requests'],45)
        self.assertEqual(len(report['Focus_r1']),40)
    def test_failed_reference_saves_proxy_and_does_not_make_fake_r1(self):
        report = self.exercise(Client(self.clock, bad_history=['X000'], root=self.output))
        self.assertEqual(report['result'], 'REFERENCE_UNAVAILABLE')
        self.assertIsNone(report['Focus_r1']); self.assertIsNone(report['common_count'])
        self.assertTrue((self.output/'vol_test.json').is_file())
        self.assertFalse((self.output/'focus').exists())
    def test_0900_candle_is_excluded_by_actual_reference_acquisition(self):
        report=self.exercise(Client(self.clock, root=self.output))
        import csv
        with (self.output/'comparison.csv').open() as stream:
            rows=list(csv.DictReader(stream))
        self.assertEqual(rows[0]['OV_DECISION'],'5000')
        self.assertEqual(rows[0]['current_volume'],'20000')
    def test_late_start_is_recorded_not_disguised_as_0900(self):
        self.clock.now=self.clock.now.replace(minute=2)
        report=self.exercise(Client(self.clock, root=self.output))
        self.assertEqual(report['proxy_start_lateness_seconds'],120)
    def test_wrong_day_refuses_before_calls_or_output(self):
        self.clock.now+=timedelta(days=1); client=Client(self.clock)
        with self.assertRaises(ValueError): self.exercise(client)
        self.assertEqual(client.calls,[]); self.assertFalse(self.output.exists())
    def test_existing_output_refuses_without_overwrite(self):
        self.output.mkdir(); (self.output/'original').write_text('keep')
        client=Client(self.clock)
        with self.assertRaises(FileExistsError): self.exercise(client)
        self.assertEqual(client.calls,[]); self.assertEqual((self.output/'original').read_text(),'keep')
    def test_reference_builder_failure_keeps_acquisition_evidence(self):
        with patch('mb_market_data.schwab_quotes.datetime',self.clock.datetime_type()),redirect_stdout(StringIO()):
            report=run_comparison(Client(self.clock,root=self.output),opening_path=self.opening,
                output_dir=self.output,snapshot_at=self.clock(),clock=self.clock,
                sleep=self.clock.sleep,request_interval=0,
                focus_builder=Mock(side_effect=RuntimeError('synthetic')))
        self.assertEqual(report['result'],'REFERENCE_UNAVAILABLE')
        self.assertTrue((self.output/'api_ov'/'manifest.json').is_file())
    def test_cli_unlocks_once_closes_once(self):
        # Test the CLI's authentication boundary without importing real credentials.
        watch=ModuleType('schwab_watchlists.api_ov_focus_production')
        for name in ('load_api_ov_evidence','select_api_ov_symbols','write_api_ov_focus_artifacts'):
            setattr(watch,name,Mock())
        package=ModuleType('schwab_watchlists'); package.__path__=[]
        secure=ModuleType('mb_tools.schwab_secure');client=Mock()
        secure.make_secure_schwab_client=Mock(return_value=client)
        helpers=ModuleType('mb_tools');helpers.__path__=[]
        ecfg=self.root/'synthetic.ecfg';ecfg.write_text('NOT REAL CREDENTIALS')
        modules={'schwab_watchlists':package,'schwab_watchlists.api_ov_focus_production':watch,
                 'mb_tools':helpers,'mb_tools.schwab_secure':secure}
        with patch.dict(sys.modules,modules),patch('getpass.getpass',return_value='SYNTHETIC') as prompt:
            namespace=runpy.run_path(str(ROOT/'probes/run_volume_proxy_comparison.py'),run_name='voltest_cli')
            main=namespace['main'];main.__globals__['datetime']=self.clock.datetime_type()
            main.__globals__['run_comparison']=Mock(return_value={'result':'COMPARISON_COMPLETE'})
            with redirect_stdout(StringIO()):
                self.assertEqual(main(['--opening-proposal',str(self.opening),'--ecfg',str(ecfg),
                                       '--output-dir',str(self.output)]),0)
            prompt.assert_called_once();secure.make_secure_schwab_client.assert_called_once()
            self.assertIs(main.__globals__['run_comparison'].call_args.args[0],client)
            client.close.assert_called_once()


class PostcloseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.clock=Clock(16,1)
    def test_broad_build_uses_supplied_client_and_existing_filters(self):
        client=Client(self.clock)
        fetch=Mock(side_effect=[NASDAQ_RAW,OTHER_RAW])
        with patch('mb_market_data.schwab_quotes.datetime',self.clock.datetime_type()),redirect_stdout(StringIO()):
            result=build_with_client(client,session_date=DAY,target_date=date(2026,11,6),
                                     output_dir=self.root/'r0',clock=self.clock,directory_fetch=fetch)
        self.assertEqual(result['candidate_symbols'],2)
        self.assertEqual(result['quote_requests'],1)
        self.assertEqual(client.calls[0][1],('DAIC','XYZ'))
        r0=load_opening_proposal(Path(result['opening_proposal']))
        self.assertEqual(r0.uni_symbols,('DAIC','XYZ'))
        self.assertEqual(r0.focus_symbols,());self.assertEqual(r0.hot_symbols,())
        self.assertEqual(client.closed,0);self.assertEqual(fetch.call_count,2)
        self.assertFalse(result['membership_published'])
    def test_normal_finish_waits_for_close_plus_delay(self):
        self.clock.now=self.clock.now.replace(hour=15,minute=59,second=31)
        builder=Mock(return_value={'status':'R0_BUILT_NOT_PUBLISHED'});client=object()
        with redirect_stdout(StringIO()):
            handoff(client,scheduled_completion=True,channel='uni',
                    window_end=self.clock.now.replace(hour=16,minute=0,second=0),
                    target_date=date(2026,11,6),output_dir=self.root/'r0',
                    clock=self.clock,sleep=self.clock.sleep,builder=builder)
        self.assertEqual(self.clock().time(),time(16,1))
        self.assertIs(builder.call_args.args[0],client);builder.assert_called_once()
    def test_manual_stop_does_not_wait_or_build(self):
        builder=Mock();sleep=Mock()
        result=handoff(object(),scheduled_completion=False,channel='uni',
            window_end=self.clock.now,target_date=date(2026,11,6),output_dir=self.root/'r0',
            clock=self.clock,sleep=sleep,builder=builder)
        self.assertEqual(result['status'],'NOT_RUN');builder.assert_not_called();sleep.assert_not_called()
    def test_focus_does_not_build_r0(self):
        builder=Mock()
        result=handoff(object(),scheduled_completion=True,channel='focus',
            window_end=self.clock.now,target_date=date(2026,11,6),output_dir=self.root/'r0',
            clock=self.clock,builder=builder)
        self.assertEqual(result['status'],'NOT_RUN');builder.assert_not_called()
    def test_before_close_refuses_without_provider_calls(self):
        self.clock.now=self.clock.now.replace(hour=15)
        fetch=Mock();client=Client(self.clock)
        with self.assertRaises(ValueError):
            build_with_client(client,session_date=DAY,target_date=date(2026,11,6),
                output_dir=self.root/'r0',clock=self.clock,directory_fetch=fetch)
        fetch.assert_not_called();self.assertEqual(client.calls,[])
    def test_midnight_cannot_relabel_new_volume_as_yesterday(self):
        self.clock.now+=timedelta(days=1);fetch=Mock()
        with self.assertRaises(ValueError):
            build_with_client(Client(self.clock),session_date=DAY,target_date=date(2026,11,6),
                output_dir=self.root/'r0',clock=self.clock,directory_fetch=fetch)
        fetch.assert_not_called()
    def test_existing_partial_build_is_preserved(self):
        output=self.root/'r0';output.mkdir();(output/'evidence').write_text('keep')
        fetch=Mock()
        with self.assertRaises(FileExistsError):
            build_with_client(Client(self.clock),session_date=DAY,target_date=date(2026,11,6),
                output_dir=output,clock=self.clock,directory_fetch=fetch)
        fetch.assert_not_called();self.assertEqual((output/'evidence').read_text(),'keep')
    def test_keyboard_interrupt_while_waiting_never_builds(self):
        self.clock.now=self.clock.now.replace(hour=15,minute=59)
        builder=Mock()
        with self.assertRaises(KeyboardInterrupt),redirect_stdout(StringIO()):
            handoff(object(),scheduled_completion=True,channel='uni',
                window_end=self.clock.now.replace(hour=16,minute=0),
                target_date=date(2026,11,6),output_dir=self.root/'r0',clock=self.clock,
                sleep=Mock(side_effect=KeyboardInterrupt),builder=builder)
        builder.assert_not_called()
    def test_hook_is_opt_in_and_executes_before_client_close(self):
        source=(ROOT/'probes/probe_universe_quote_watch.py').read_text()
        start=source.index("manifest['postclose_r0'] = handoff(")
        self.assertGreater(source.index('client.close()',start),start)
        self.assertIn('postclose_target is not None and scheduled_completion',source)
        self.assertIn("getattr(args, 'postclose_target_date', None)",source)


if __name__=='__main__':unittest.main()
