from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from mb_market_data.quote_observation_store import QuoteObservationStore
from mb_market_data.quote_journal_audit import audit_quote_journal
from mb_market_data.sampling_membership import SamplingChannel, SamplingHierarchyRevision
from mb_market_data.sampling_acquisition import (MARKER, AcquisitionBusy, AcquisitionSettings, ExplicitPollSlot,
    IsolatedSamplingPoller, next_slot, owner_lock)
from mb_market_data.watchlist_polling import POLL_SECONDS, PollWindow, WatchlistKind


class Response:
    ok = True
    status_code = 200
    def __init__(self, payload): self.payload = payload
    def json(self): return self.payload


class SyntheticClient:
    def __init__(self, callback=None): self.calls = []; self.callback = callback
    def quotes(self, symbols, *, fields):
        self.calls.append((list(symbols), fields))
        if self.callback:
            return self.callback(symbols, fields)
        return Response({s: {"assetMainType": "EQUITY", "realtime": False,
                            "quote": {"lastPrice": 100.0 + len(self.calls), "totalVolume": len(self.calls),
                                      "securityStatus": "Synthetic offline quote"}} for s in symbols})


def fixture(folder, *, hot=("MSFT",), real_clock=False, start=None, duration=120, channel=SamplingChannel.HOT, seconds=tuple(range(0,60,2))):
    base = start or (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
    clock = [base]
    get_clock = (lambda: datetime.now(timezone.utc)) if real_clock else (lambda: clock[0])
    window = PollWindow(base, base + timedelta(seconds=duration))
    store = QuoteObservationStore(folder / "quotes.sqlite3", session_date=window.session_date, schema_version=2)
    store.initialize()
    baseline = SamplingHierarchyRevision(session_date=window.session_date, revision=0,
        effective_at=base - timedelta(microseconds=1), uni_symbols=("AAPL","MSFT","SPY"),
        focus_symbols=tuple(sorted({"SPY",*hot})), hot_symbols=hot, source="isolated_hot_fixture",
        metadata={MARKER: True, "experimental": True, "quote_source": "synthetic", "logical_clock": not real_clock})
    store.record_membership_revision(baseline)
    settings = AcquisitionSettings(channel, seconds, window, 2, "all", "synthetic_offline", 0.5, 0.05)
    client = SyntheticClient()
    poller = IsolatedSamplingPoller(store, settings=settings, client=client, clock=get_clock)
    poller.initialize()
    return store, poller, client, clock


def fail_at(expected):
    def hook(stage):
        if stage == expected: raise RuntimeError("injected interruption " + stage)
    return hook


class HotAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.store, self.poller, self.client, self.clock = fixture(self.folder)
        self.base = self.clock[0]
        clock = self.clock
        class LogicalDatetime(datetime):
            @classmethod
            def now(cls, tz=None): return clock[0].astimezone(tz or timezone.utc)
        self.patcher = patch("mb_market_data.schwab_quotes.datetime", LogicalDatetime)
        self.patcher.start(); self.addCleanup(self.patcher.stop)

    def at(self, seconds): self.clock[0] = self.base + timedelta(seconds=seconds)
    def slot(self, seconds=0):
        return ExplicitPollSlot(self.poller.settings.channel, self.base + timedelta(seconds=seconds), self.poller.settings.seconds)
    def reopen(self):
        self.poller = IsolatedSamplingPoller(self.store, settings=self.poller.settings, client=self.client, clock=lambda:self.clock[0])
        self.poller.initialize(); return self.poller
    def revise(self, effective, hot=(), focus=("SPY",)):
        old = self.store.membership_revisions_in_effective_order()[-1]
        value = replace(old, revision=old.revision+1, effective_at=self.base+timedelta(seconds=effective),
                        hot_symbols=hot, focus_symbols=focus, published_at=None, source="synthetic_test_change")
        self.store.record_membership_revision(value)
    def sample(self, seconds=0, **kwargs): return self.poller.sample(self.slot(seconds), **kwargs)

    def test_hot_grid_is_explicit_and_existing_cadences_are_unchanged(self):
        self.assertEqual(POLL_SECONDS, {WatchlistKind.UNI:(0,30),WatchlistKind.FOCUS:(5,20,35,50)})
        self.assertEqual(tuple(WatchlistKind), (WatchlistKind.UNI,WatchlistKind.FOCUS))
        self.assertEqual(next_slot(self.poller.settings, after=self.base-timedelta(microseconds=1)).scheduled_at,self.base)
        self.assertEqual(next_slot(self.poller.settings, after=self.base).scheduled_at,self.base+timedelta(seconds=2))

    def test_captured_hot_membership_and_real_collector_path_are_journaled(self):
        self.assertEqual(self.sample()["status"],"acquired")
        a=self.store.acquisitions_in_replay_order()[0]
        self.assertEqual((a.channel,a.channel_revision,a.requested_symbol_count),("hot",0,1))
        o=self.store.get_observations(a.acquisition_id)[0]
        self.assertEqual((o.symbol,o.status,o.values["quote_last_price"]),("MSFT","quote",101))
        self.assertEqual(self.client.calls,[(["MSFT"],"all")])

    def test_empty_hot_is_a_durable_skip_without_quote_request(self):
        self.revise(0)
        self.assertEqual(self.sample()["status"],"empty_membership")
        self.assertEqual(self.client.calls,[])
        with closing(sqlite3.connect(self.store.database_path)) as c:
            self.assertEqual(c.execute("SELECT channel,channel_revision,reason FROM poll_slot_skip").fetchone(),("hot",1,"empty_membership"))

    def test_dispatch_uses_slot_time_membership_despite_later_revision(self):
        self.revise(0.1,hot=("AAPL",),focus=("AAPL","SPY"))
        self.at(0.2); self.sample()
        self.assertEqual(self.client.calls[0][0],["MSFT"])
        self.assertEqual(self.store.acquisitions_in_replay_order()[0].channel_revision,0)

    def test_membership_changed_while_request_is_in_flight_does_not_rebind_result(self):
        def reply(symbols,fields):
            self.revise(1); self.at(3)
            return Response({"MSFT":{"quote":{"lastPrice":17}}})
        self.client.callback=reply; self.sample()
        a=self.store.acquisitions_in_replay_order()[0]
        self.assertEqual((a.channel_revision,a.scheduled_at_utc),(0,self.base))
        self.at(4); self.assertEqual(self.sample(4)["status"],"empty_membership")
        self.assertEqual(len(self.client.calls),1)

    def test_full_batching_follows_captured_symbol_order(self):
        self.revise(0,hot=("AAPL","MSFT","SPY"),focus=("AAPL","MSFT","SPY"))
        self.sample()
        self.assertEqual([c[0] for c in self.client.calls],[["AAPL","MSFT"],["SPY"]])
        a=self.store.acquisitions_in_replay_order()[0]
        self.assertEqual((a.request_count,a.requested_symbol_count),(2,3))
        self.assertEqual([o.symbol for o in self.store.get_observations(a.acquisition_id)],["AAPL","MSFT","SPY"])

    def test_invalid_missing_and_request_error_are_explicit_observations(self):
        self.revise(0,hot=("AAPL","MSFT","SPY"),focus=("AAPL","MSFT","SPY"))
        def reply(symbols,fields):
            if symbols==["SPY"]: raise TimeoutError("synthetic timeout")
            return Response({"errors":{"invalidSymbols":["AAPL"]}})
        self.client.callback=reply; self.sample()
        a=self.store.acquisitions_in_replay_order()[0]
        self.assertEqual([o.status for o in self.store.get_observations(a.acquisition_id)],["invalid","missing","request_error"])

    def test_restart_after_assembly_salvages_without_second_quote_request(self):
        with self.assertRaises(RuntimeError):self.sample(fault_hook=fail_at("after_assembled"))
        self.assertEqual(len(self.client.calls),1); self.assertEqual(self.store.acquisitions_in_replay_order(),())
        self.at(5); self.reopen().recover()
        self.assertEqual(len(self.client.calls),1); self.assertEqual(len(self.store.acquisitions_in_replay_order()),1)
        self.assertIsNone(self.poller.snapshot()["pending"])

    def test_restart_after_journal_commit_retains_original_receipt_without_api_repeat(self):
        with self.assertRaises(RuntimeError):self.sample(fault_hook=fail_at("after_journal_commit"))
        old=self.store.acquisitions_in_replay_order()[0]
        self.at(5); self.reopen().recover()
        self.assertEqual(self.store.acquisitions_in_replay_order(),(old,)); self.assertEqual(len(self.client.calls),1)
        self.assertTrue(self.poller.snapshot()["events"][-1]["recovered"])

    def test_duplicate_slot_and_restart_after_ack_do_not_query_again(self):
        self.sample();self.assertEqual(self.sample()["status"],"already_finished")
        self.reopen();self.assertEqual(self.sample()["status"],"already_finished")
        self.assertEqual(len(self.client.calls),1)

    def test_unknown_dispatched_outcome_is_retained_as_gap_and_never_reissued(self):
        with self.assertRaises(RuntimeError):self.sample(fault_hook=fail_at("after_dispatch"))
        self.at(5);self.reopen()
        self.assertEqual(self.poller.recover()["status"],"unresolved_dispatch")
        self.assertEqual(self.sample()["status"],"already_finished")
        self.assertEqual(self.client.calls,[])
        self.assertEqual(self.poller.snapshot()["events"][-1]["kind"],"abandoned")

    def test_received_but_unsaved_response_remains_explicit_gap_without_repeat(self):
        with self.assertRaises(RuntimeError):self.sample(fault_hook=fail_at("after_response"))
        self.assertEqual(len(self.client.calls),1)
        self.at(5);self.reopen()
        self.assertEqual(self.poller.recover()["status"],"unresolved_dispatch")
        self.assertEqual(len(self.client.calls),1)
        self.assertEqual(self.store.acquisitions_in_replay_order(),())

    def test_empty_skip_restart_is_recoverable_without_quote_call(self):
        self.revise(0)
        with self.assertRaises(RuntimeError):self.sample(fault_hook=fail_at("after_dispatch"))
        self.at(1);self.reopen().recover()
        self.assertEqual(self.poller.snapshot()["events"][-1]["journal_receipt"]["kind"],"empty_membership")
        self.assertEqual(self.client.calls,[])

    def test_failed_journal_write_preserves_received_result_for_retry(self):
        original=self.store.record_acquisition
        with patch.object(self.store,"record_acquisition",side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaises(sqlite3.OperationalError):self.sample()
        self.assertEqual(self.poller.snapshot()["events"][-1]["kind"],"write_failed")
        self.at(2);self.reopen().recover()
        self.assertEqual(len(self.client.calls),1);self.assertEqual(len(self.store.acquisitions_in_replay_order()),1)

    def test_partial_observation_write_rolls_back_before_salvage(self):
        with closing(sqlite3.connect(self.store.database_path)) as c,c:
            c.execute("CREATE TRIGGER fail_observation BEFORE INSERT ON quote_observation BEGIN SELECT RAISE(ABORT,'injected observation failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.sample()
        self.assertEqual(self.store.acquisitions_in_replay_order(),())
        with closing(sqlite3.connect(self.store.database_path)) as c,c:c.execute("DROP TRIGGER fail_observation")
        self.reopen().recover();self.assertEqual(len(self.client.calls),1)

    def test_late_slot_is_reported_without_catchup_quote_request(self):
        self.at(3);self.assertEqual(self.sample()["status"],"missed")
        self.assertEqual(self.client.calls,[])
        self.assertEqual(next_slot(self.poller.settings,after=self.clock[0]).scheduled_at,self.base+timedelta(seconds=4))

    def test_dispatch_budget_passing_after_spool_prevents_api(self):
        def hook(stage):
            if stage=="after_dispatch":self.at(1)
        self.assertEqual(self.sample(fault_hook=hook)["status"],"missed")
        self.assertEqual(self.client.calls,[])

    def test_future_slot_wrong_channel_or_wrong_grid_is_rejected(self):
        with self.assertRaisesRegex(ValueError,"future"):self.sample(2)
        with self.assertRaises(ValueError):self.poller.sample(ExplicitPollSlot(SamplingChannel.UNI,self.base,(0,30)))
        with self.assertRaises(ValueError):ExplicitPollSlot(SamplingChannel.HOT,self.base+timedelta(seconds=1),(0,2))

    def test_window_end_is_excluded(self):
        self.assertIsNone(next_slot(self.poller.settings,after=self.poller.settings.window.end_at))
        self.at(120)
        with self.assertRaises(ValueError):self.sample(120)

    def test_changed_evidence_chain_is_refused_before_another_quote_request(self):
        self.sample()
        with closing(sqlite3.connect(self.poller.path)) as c,c:c.execute("UPDATE acquisition_event SET payload='{}' WHERE sequence=1")
        self.at(2)
        with self.assertRaisesRegex(ValueError,"chain"):self.sample(2)
        self.assertEqual(len(self.client.calls),1)

    def test_changed_configuration_cannot_reopen_ledger(self):
        other=IsolatedSamplingPoller(self.store,settings=replace(self.poller.settings,batch_size=1),client=self.client,clock=lambda:self.clock[0])
        with self.assertRaisesRegex(ValueError,"identity/settings"):other.initialize()

    def test_deleted_acquisition_is_detected_by_saved_acknowledgement(self):
        self.sample()
        with closing(sqlite3.connect(self.store.database_path)) as c,c:
            c.execute("DELETE FROM quote_observation");c.execute("DELETE FROM quote_acquisition")
        with self.assertRaisesRegex(ValueError,"acknowledgement"):self.poller.snapshot()

    def test_changed_normalized_quote_is_detected_against_durable_response(self):
        self.sample()
        with closing(sqlite3.connect(self.store.database_path)) as c,c:
            c.execute("UPDATE quote_observation SET quote_last_price=999")
        with self.assertRaisesRegex(ValueError,"observations differ"):self.poller.snapshot()

    def test_two_instances_serialize_through_the_channel_sidecar(self):
        other=self.reopen()
        with owner_lock(other.lock_path):
            with self.assertRaises(AcquisitionBusy):other.sample(self.slot())
        self.sample();self.assertEqual(len(self.client.calls),1)

    def test_stop_interrupts_wait_for_future_slot(self):
        self.poller.clock=lambda:self.base-timedelta(seconds=20)
        stop=threading.Event();errors=[]
        def run():
            try:self.poller.run_loop(stop)
            except BaseException as e:errors.append(e)
        thread=threading.Thread(target=run);thread.start();stop.set();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertEqual(errors,[]);self.assertEqual(self.client.calls,[])

    def test_production_journal_and_missing_marker_are_refused(self):
        old=self.store.database_path
        self.store.database_path=self.folder/'quote_observation_journal_v2_api_opening_2026-10-19'/'x.sqlite3'
        with self.assertRaisesRegex(ValueError,"Production"):IsolatedSamplingPoller(self.store,settings=self.poller.settings,client=self.client)
        self.store.database_path=old
        original=self.store.membership_revisions_in_effective_order
        value=replace(original()[0],metadata={})
        with patch.object(self.store,"membership_revisions_in_effective_order",return_value=(value,)):
            with self.assertRaisesRegex(ValueError,"marker"):IsolatedSamplingPoller(self.store,settings=self.poller.settings,client=self.client)

    def test_invalid_settings_are_rejected(self):
        for change in ({"seconds":()}, {"seconds":(1,1)}, {"seconds":(True,)}, {"seconds":(60,)},
                       {"batch_size":501},{"max_dispatch_lateness_seconds":float("nan")},{"retry_seconds":0}):
            with self.subTest(change=change),self.assertRaises(ValueError):replace(self.poller.settings,**change)

    def test_missing_hierarchy_fails_closed_without_faking_empty_hot(self):
        with patch.object(self.store,"latest_membership_revision_effective_at",return_value=None):
            with self.assertRaisesRegex(ValueError,"No hierarchy"):self.sample()
        self.assertEqual(self.client.calls,[])
        self.assertEqual(self.poller.snapshot()["events"],[])

    def test_late_gap_remains_visible_to_existing_auditor(self):
        self.at(1);self.sample()
        audit=audit_quote_journal(self.store.database_path,audited_at=self.base+timedelta(seconds=1))
        self.assertFalse(audit.passed)
        self.assertTrue(any(f.code=="missing_poll_slots" for f in audit.findings))

    def test_hot_write_failure_leaves_uni_and_focus_able_to_commit(self):
        others=[]
        for channel in (SamplingChannel.UNI,SamplingChannel.FOCUS):
            s=replace(self.poller.settings,channel=channel)
            p=IsolatedSamplingPoller(self.store,settings=s,client=SyntheticClient(),clock=lambda:self.clock[0]);p.initialize();others.append(p)
        write=self.store.record_acquisition
        def blocked(run,req,result,**kwargs):
            if req.watchlist_kind==SamplingChannel.HOT:raise sqlite3.OperationalError("synthetic Hot write interruption")
            return write(run,req,result,**kwargs)
        with patch.object(self.store,"record_acquisition",side_effect=blocked):
            with self.assertRaises(sqlite3.OperationalError):self.sample()
            for p in others:p.sample(ExplicitPollSlot(p.settings.channel,self.base,p.settings.seconds))
        self.assertEqual({a.channel for a in self.store.acquisitions_in_replay_order()},{"uni","focus"})
        self.poller.recover()
        self.assertEqual({a.channel for a in self.store.acquisitions_in_replay_order()},{"uni","focus","hot"})
        self.assertEqual(len(self.client.calls),1)


if __name__=="__main__":unittest.main()
