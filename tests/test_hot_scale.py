from __future__ import annotations
import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch
import test_sampling_acquisition as accepted
from mb_market_data.sampling_acquisition import canonical,digest,IsolatedSamplingPoller
from mb_market_data.hot_provider_trial import TrialSettings,growth_timings


class ScaleIntegrityTests(unittest.TestCase):
    setUp=accepted.HotAcquisitionTests.setUp
    at=accepted.HotAcquisitionTests.at
    slot=accepted.HotAcquisitionTests.slot
    sample=accepted.HotAcquisitionTests.sample
    reopen=accepted.HotAcquisitionTests.reopen

    def test_warm_normalization_does_not_skip_live_observation_validation_before_api(self):
        self.sample(); self.assertTrue(self.poller._normalization_cache)
        with closing(sqlite3.connect(self.store.database_path)) as c,c:
            c.execute("UPDATE quote_observation SET quote_last_price=999")
        self.at(2)
        with self.assertRaisesRegex(ValueError,"observations differ"): self.sample(2)
        self.assertEqual(len(self.client.calls),1)

    def test_recomputed_assembled_hash_does_not_reuse_old_normalization(self):
        self.sample()
        with closing(sqlite3.connect(self.poller.path)) as c,c:
            rows=list(c.execute("SELECT sequence,payload FROM acquisition_event ORDER BY sequence"))
            previous=digest(self.poller.identity)
            for seq,raw in rows:
                value=json.loads(raw)
                if value['kind']=='assembled': value['result']['results'][0]['quote']['quote']['lastPrice']=999
                sha=digest({'sequence':seq,'payload':value,'previous_sha256':previous})
                c.execute('UPDATE acquisition_event SET payload=?,previous_sha256=?,event_sha256=? WHERE sequence=?',(canonical(value),previous,sha,seq))
                previous=sha
        with self.assertRaisesRegex(ValueError,'observations differ'): self.poller.snapshot()
        self.assertEqual(len(self.client.calls),1)

    def test_warm_cache_reuses_only_derived_values_and_can_be_discarded(self):
        self.sample()
        with patch.object(self.store,'_observation_values',wraps=self.store._observation_values) as normalize:
            self.poller.snapshot(); self.assertEqual(normalize.call_count,0)
            self.poller._normalization_cache.clear()
            self.poller.snapshot(); self.assertEqual(normalize.call_count,1)
        self.assertGreater(self.poller.normalization_cache_hits,0)

    def test_restart_rebuilds_normalization_from_committed_evidence(self):
        self.sample()
        self.reopen()
        self.assertEqual(self.poller.normalization_cache_misses,1)
        self.assertEqual(self.poller.snapshot()['pending'],None)
        self.assertEqual(len(self.client.calls),1)

    def test_historical_scan_reuses_journal_connection_without_skipping_rows(self):
        for n in range(5): self.at(n*2); self.sample(n*2)
        with patch.object(self.store,'_connect',wraps=self.store._connect) as connect:
            self.poller.snapshot()
        self.assertLessEqual(connect.call_count,4)
        self.assertGreater(self.poller.verification_reads,0)

    def test_larger_plan_has_explicit_bounds_and_growth_groups(self):
        value=TrialSettings(tuple('S'+str(i) for i in range(40)),(2,4),300,225,3,'synthetic')
        self.assertEqual(value.planned_calls,225)
        for symbols,seconds,budget in ((tuple('S'+str(i) for i in range(81)),300,225),(('SPY',),604,600),(('SPY',),300,601)):
            with self.assertRaises(ValueError):TrialSettings(symbols,(2,4),seconds,budget,3,'synthetic')
        result=growth_timings([{'cycle_seconds':i/1000,'verification_seconds':i/2000} for i in range(1,10)])
        self.assertEqual([v['cycle_seconds']['count'] for v in result.values()],[3,3,3])
        self.assertEqual(result['last_third']['cycle_seconds']['mean_ms'],8)
