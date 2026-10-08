from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from mb_market_data.quote_observation_store import (
    DuplicateRecordError, PollRunProvenance, QuoteObservationStore, RecordResult,
)
from mb_market_data.sampling_membership import SamplingHierarchyRevision
from mb_market_data.schwab_quotes import QuoteBatchResult, QuoteResult, QuoteStatus
from mb_market_data.sqlite_write_recovery import WriteRetryExhausted, WriteRetryPolicy
from mb_market_data.watchlist_polling import PollRequest, PollSlot, SkippedPollSlot, WatchlistKind

UTC = timezone.utc
OPEN = datetime(2026, 10, 8, 13, 30, tzinfo=UTC)


def sqlite_error(code: int) -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("injected database fault")
    error.sqlite_errorcode = code
    error.sqlite_errorname = next((name for name in ("SQLITE_BUSY", "SQLITE_BUSY_SNAPSHOT", "SQLITE_LOCKED", "SQLITE_IOERR")
                                  if getattr(sqlite3, name) == code), "UNKNOWN")
    return error


class WriteRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "2026-10-08.sqlite3"
        self.events = []
        self.store = QuoteObservationStore(
            self.path, session_date=date(2026, 10, 8), schema_version=2,
            write_retry_policy=WriteRetryPolicy(total_seconds=1, attempt_seconds=.02,
                                               initial_delay_seconds=.01, maximum_delay_seconds=.02),
            write_event_handler=self.events.append,
        )
        self.store.initialize()
        with patch("mb_market_data.quote_observation_store._utc_now", return_value=OPEN - timedelta(seconds=1)):
            self.store.record_membership_revision(SamplingHierarchyRevision(
                session_date=date(2026, 10, 8), revision=0, effective_at=OPEN,
                uni_symbols=("AAPL", "MSFT", "SPY"), focus_symbols=("MSFT",),
                hot_symbols=(), source="synthetic-contention-test",
            ))
        for kind in ("uni", "focus"):
            self.store.record_run(PollRunProvenance(
                run_id=kind, started_at=OPEN, software_version="synthetic",
                configuration={"watchlist_kind": kind},
            ))

    def request(self, kind="uni", seconds=0):
        when = OPEN + timedelta(seconds=seconds)
        return PollRequest(
            slot=PollSlot(WatchlistKind(kind), when), watchlist_revision=0,
            symbols=("AAPL", "MSFT", "SPY") if kind == "uni" else ("MSFT",),
            dispatched_at=when + timedelta(milliseconds=1),
        )

    def result(self, request):
        return QuoteBatchResult(
            results=tuple(QuoteResult(
                symbol=symbol, status=QuoteStatus.QUOTE,
                quote={"symbol": symbol, "quote": {"lastPrice": 12.5, "totalVolume": 100}},
                detail=None, batch_number=1,
                request_started_at_utc=request.dispatched_at,
                response_received_at_utc=request.dispatched_at + timedelta(milliseconds=10),
            ) for symbol in request.symbols), request_count=1, batch_size=400, unexpected_symbols=(),
        )

    def record(self, request, result=None):
        return self.store.record_acquisition(
            request.watchlist_kind.value, request, result or self.result(request),
            completed_at=request.dispatched_at + timedelta(milliseconds=20),
        )

    def counts(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                         for table in ("quote_acquisition", "quote_observation"))

    def hold_lock(self):
        ready, release = threading.Event(), threading.Event()
        def hold():
            with closing(sqlite3.connect(self.path)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                ready.set()
                release.wait(3)
                connection.rollback()
        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(ready.wait(1))
        self.addCleanup(thread.join, 3)
        self.addCleanup(release.set)
        return release

    def test_temporary_lock_retries_original_sample_once_and_keeps_provenance(self):
        release = self.hold_lock()
        def notify(event):
            self.events.append(event)
            if event["state"] == "RETRYING" and event["attempts"] == 2:
                release.set()
        self.store.write_event_handler = notify
        request = self.request()
        payload = self.result(request)
        self.assertEqual(self.record(request, payload), RecordResult.INSERTED)
        self.assertEqual(self.counts(), (1, 3))
        stored = self.store.get_acquisition(request.batch_id)
        self.assertEqual(stored.dispatched_at_utc, request.dispatched_at)
        self.assertEqual(stored.completed_at_utc, request.dispatched_at + timedelta(milliseconds=20))
        self.assertEqual(self.events[-1]["state"], "COMMITTED")
        self.assertEqual(self.events[-1]["attempts"], 3)
        self.assertTrue(all(event["identity"]["acquisition_id"] == request.batch_id for event in self.events))
        self.assertEqual(self.record(request, payload), RecordResult.ALREADY_PRESENT)
        self.assertEqual(self.counts(), (1, 3))

    def test_persistent_lock_exhausts_deadline_without_partial_records(self):
        self.hold_lock()
        self.store.write_retry_policy = replace(self.store.write_retry_policy, total_seconds=.12)
        began = time.monotonic()
        with self.assertRaises(WriteRetryExhausted) as caught:
            self.record(self.request())
        self.assertLess(time.monotonic() - began, .7)
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
        self.assertEqual(caught.exception.diagnostic["identity"]["phase"], "BEGIN")
        self.assertEqual(self.events[-1]["state"], "EXHAUSTED")

    def test_attempt_limit_is_independent_of_long_deadline(self):
        self.hold_lock()
        self.store.write_retry_policy = replace(self.store.write_retry_policy, total_seconds=10, max_attempts=2)
        with self.assertRaises(WriteRetryExhausted) as caught:
            self.record(self.request())
        self.assertEqual(caught.exception.diagnostic["attempts"], 2)
        self.assertLess(caught.exception.diagnostic["elapsed_seconds"], .5)

    def test_mid_transaction_busy_rolls_back_before_repeating(self):
        real_connect = sqlite3.connect
        state = {"inject": True}
        class FaultOnce(sqlite3.Connection):
            def executemany(connection, sql, rows):
                if "INSERT INTO quote_observation" in sql and state["inject"]:
                    state["inject"] = False
                    super().executemany(sql, list(rows)[:1])
                    raise sqlite_error(sqlite3.SQLITE_BUSY_SNAPSHOT)
                return super().executemany(sql, rows)
        def connect(*args, **kwargs):
            return real_connect(*args, **kwargs, factory=FaultOnce)
        def notify(event):
            self.events.append(event)
            if event["state"] == "RETRYING":
                self.assertEqual(self.counts(), (0, 0))
        self.store.write_event_handler = notify
        with patch("mb_market_data.quote_observation_store.sqlite3.connect", side_effect=connect):
            self.record(self.request())
        self.assertEqual(self.counts(), (1, 3))
        self.assertEqual(self.events[0]["identity"]["phase"], "WRITE")
        self.assertEqual(self.events[-1]["sqlite_errorcode"], sqlite3.SQLITE_BUSY_SNAPSHOT)

    def test_commit_busy_rolls_back_then_confirms_one_commit(self):
        real_connect = sqlite3.connect
        state = {"inject": True}
        class FaultOnce(sqlite3.Connection):
            def __exit__(connection, exc_type, exc, traceback):
                if exc_type is None and state["inject"]:
                    state["inject"] = False
                    connection.rollback()
                    raise sqlite_error(sqlite3.SQLITE_BUSY)
                return super().__exit__(exc_type, exc, traceback)
        with patch("mb_market_data.quote_observation_store.sqlite3.connect",
                   side_effect=lambda *args, **kwargs: real_connect(*args, **kwargs, factory=FaultOnce)):
            self.record(self.request())
        self.assertEqual(self.counts(), (1, 3))
        self.assertEqual(self.events[0]["identity"]["phase"], "COMMIT")

    def test_non_contention_error_and_same_connection_lock_are_not_retried(self):
        real_connect = sqlite3.connect
        for code in (sqlite3.SQLITE_IOERR, sqlite3.SQLITE_LOCKED):
            self.events.clear()
            class Broken(sqlite3.Connection):
                def execute(connection, sql, *args):
                    if sql == "BEGIN IMMEDIATE":
                        raise sqlite_error(code)
                    return super().execute(sql, *args)
            with patch("mb_market_data.quote_observation_store.sqlite3.connect",
                       side_effect=lambda *args, **kwargs: real_connect(*args, **kwargs, factory=Broken)):
                with self.assertRaises(sqlite3.OperationalError):
                    self.record(self.request())
            self.assertEqual([(event["state"], event["attempts"]) for event in self.events], [("FAILED", 1)])
            self.assertEqual(self.counts(), (0, 0))

    def test_conflicting_payload_still_refused_after_successful_retry(self):
        release = self.hold_lock()
        self.store.write_event_handler = lambda event: release.set()
        request = self.request()
        payload = self.result(request)
        self.record(request, payload)
        changed = replace(payload, request_count=2)
        with self.assertRaises(DuplicateRecordError):
            self.record(request, changed)
        self.assertEqual(self.counts(), (1, 3))

    def test_uni_and_focus_retry_concurrently_into_same_journal(self):
        release = self.hold_lock()
        lock = threading.Lock()
        seen = set()
        def notify(event):
            with lock:
                self.events.append(event)
                seen.add(event["identity"]["channel"])
                if seen == {"uni", "focus"}:
                    release.set()
        self.store.write_event_handler = notify
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(self.record, (self.request("uni"), self.request("focus", 5))))
        self.assertEqual(results, (RecordResult.INSERTED, RecordResult.INSERTED))
        self.assertEqual(self.counts(), (2, 4))
        self.assertEqual({event["identity"]["channel"] for event in self.events if event["state"] == "COMMITTED"}, {"uni", "focus"})

    def test_empty_membership_skip_uses_same_recovery_without_quote_rows(self):
        with patch("mb_market_data.quote_observation_store._utc_now", return_value=OPEN - timedelta(seconds=1)):
            self.store.record_membership_revision(SamplingHierarchyRevision(
                session_date=date(2026, 10, 8), revision=1, effective_at=OPEN,
                uni_symbols=("AAPL", "MSFT", "SPY"), focus_symbols=(),
                hot_symbols=(), source="synthetic-empty-focus",
            ))
        release = self.hold_lock()
        self.store.write_event_handler = lambda event: release.set()
        skipped = SkippedPollSlot(slot=PollSlot(WatchlistKind.FOCUS, OPEN + timedelta(seconds=5)), watchlist_revision=1,
                                  membership_effective_at=OPEN, observed_at=OPEN + timedelta(seconds=5),
                                  reason="empty_membership")
        self.assertEqual(self.store.record_skipped_slot("focus", skipped), RecordResult.INSERTED)
        self.assertEqual(self.counts(), (0, 0))

    def test_invalid_policy_is_refused(self):
        for kwargs in ({"total_seconds": 0}, {"attempt_seconds": float("inf")}, {"max_attempts": True}):
            with self.assertRaises(ValueError):
                WriteRetryPolicy(**kwargs)


if __name__ == "__main__":
    unittest.main()
