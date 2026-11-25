"""Offline tests for optional controller-owned OHLCV capture; no network."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from mb_market_data.observation_overlay import ET
from mb_market_data.overlay_live_ohlcv import LiveOHLCVReader
from mb_market_data.overlay_ohlcv_worker import (
    OwnedOHLCVSelection, configure_ohlcv, owned_ohlcv_service,
)
from mb_market_data.daily_runtime import BackoffClient
from test_overlay_live_ohlcv import StubClient, raw_candle

UTC = timezone.utc
DAY = date(2026, 11, 25)


def et(hour, minute, second=0):
    return datetime(2026, 11, 25, hour, minute, second, tzinfo=ET)


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


class OwnedOHLCVWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = Path(self.tmp.name) / "market-data"
        data.mkdir()
        root = data / "output" / "daily_session" / str(DAY)
        root.mkdir(parents=True)
        self.root = root
        self.clock = Clock(et(9, 35, 5))
        self.plan = configure_ohlcv({
            "data_root": str(data),
            "state_root": str(root),
            "session_date": DAY.isoformat(),
            "opens_at": et(9, 30).isoformat(),
            "closes_at": et(16, 0).isoformat(),
            "opening_content_sha256": "a" * 64,
            "journal": str(data / "output" / "journal" / "2026-11-25.sqlite3"),
        })
        self.owner = 45678
        self.heartbeat()

    def heartbeat(self, pid=None):
        (self.root / "owner.json").write_text(
            json.dumps({
                "pid": self.owner if pid is None else pid,
                "at": self.clock().isoformat(),
                "active": True,
            }), encoding="utf-8",
        )

    def bridge(self, **kwargs):
        return OwnedOHLCVSelection(
            self.plan, owner_pid=self.owner, clock=self.clock, **kwargs,
        )

    def fixture(self):
        return StubClient({
            "symbol": "TEST",
            "candles": [raw_candle(9, 30, 100), raw_candle(9, 35, 200)],
        })

    def test_configuration_is_opt_in_and_path_bound(self):
        self.assertTrue(self.plan["overlay_ohlcv_enabled"])
        self.assertEqual(
            Path(self.plan["overlay_ohlcv_selection_root"]),
            self.root / "ohlcv",
        )
        self.assertEqual(
            Path(self.plan["overlay_ohlcv_evidence_root"]),
            Path(self.plan["data_root"]) / "output" / "overlay_ohlcv_live",
        )
        self.assertEqual(self.plan["overlay_ohlcv_request_budget"], 2)
        self.assertFalse((self.root / "ohlcv").exists())

    def test_selection_handoff_contains_no_credentials(self):
        sink = self.bridge()
        path = sink.publish(" test ")
        self.assertEqual(sink.read(), "TEST")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["symbol"], "TEST")
        self.assertEqual(data["owner_pid"], self.owner)
        self.assertEqual(data["opening_content_sha256"], "a" * 64)
        text = path.read_text(encoding="utf-8").lower()
        self.assertNotIn("password", text)
        self.assertNotIn("access_token", text)
        self.assertNotIn("encrypted", text)

    def test_replaced_owner_blocks_new_selection(self):
        sink = self.bridge()
        sink.publish("TEST")
        self.heartbeat(pid=self.owner + 1)
        with self.assertRaisesRegex(ValueError, "authorized"):
            sink.publish("MSFT")
        self.assertEqual(sink.read(), "TEST")

    def test_stop_and_outside_session_block_selection(self):
        sink = self.bridge()
        (self.root / "stop.request").write_text("stop", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "authorized"):
            sink.publish("TEST")
        (self.root / "stop.request").unlink()
        self.clock.at = et(16, 1)
        with self.assertRaisesRegex(ValueError, "outside"):
            sink.publish("TEST")
        self.assertFalse((self.root / "ohlcv").exists())

    def test_mismatched_selection_identity_and_symbol_are_ignored(self):
        sink = self.bridge()
        path = sink.publish("TEST")
        value = json.loads(path.read_text())
        value["owner_pid"] = self.owner + 1
        path.write_text(json.dumps(value))
        self.assertIsNone(sink.read())
        value["owner_pid"] = self.owner
        value["opening_content_sha256"] = "b" * 64
        path.write_text(json.dumps(value))
        self.assertIsNone(sink.read())
        value["opening_content_sha256"] = "a" * 64
        value["symbol"] = "../BADDATA"
        path.write_text(json.dumps(value))
        self.assertIsNone(sink.read())

    def test_owned_worker_publishes_completed_snapshots_and_exits(self):
        self.bridge().publish("TEST")
        client = self.fixture()
        calls = [0]

        def stop():
            return calls[0] >= 2

        def sleep(seconds):
            self.assertEqual(seconds, 2)
            calls[0] += 1
            self.clock.at += timedelta(minutes=5)
            self.heartbeat()

        code = owned_ohlcv_service(
            self.plan, client, owner_pid=self.owner, stop=stop,
            clock=self.clock, sleep=sleep,
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(client.requests[0][1]["endDate"], et(9, 35, 5))
        self.assertEqual(client.requests[1][1]["endDate"], et(9, 40, 5))
        reader = LiveOHLCVReader(
            self.plan["overlay_ohlcv_evidence_root"], session_date=DAY,
        )
        last = reader.latest("TEST", now=et(9, 45).astimezone(UTC))
        self.assertEqual([item.volume for item in last.candles], [100, 200])
        status = json.loads((self.root / "ohlcv" / "status.json").read_text())
        self.assertEqual(status["state"], "PUBLISHED")
        self.assertEqual(status["owner_pid"], self.owner)
        self.assertEqual(status["candle_count"], 2)
        self.assertEqual(status["symbol"], "TEST")

    def test_stop_request_prevents_provider_calls(self):
        self.bridge().publish("TEST")
        (self.root / "stop.request").write_text("stop", encoding="utf-8")
        client = self.fixture()
        code = owned_ohlcv_service(
            self.plan, client, owner_pid=self.owner,
            stop=lambda: False, clock=self.clock, sleep=lambda _: None,
        )
        self.assertEqual(code, 0)
        self.assertEqual(client.requests, [])

    def test_price_history_backoff_avoids_repeated_rate_limited_calls(self):
        client = self.fixture()
        client.response.status_code = 429
        client.response.headers = {"Retry-After": "60"}
        client.response.ok = False
        current = [0.0]
        wrapper = BackoffClient(client, clock=lambda: current[0])
        response = wrapper.price_history("TEST", frequency=5)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(wrapper.retry_at, 60.0)
        with self.assertRaisesRegex(RuntimeError, "provider backoff"):
            wrapper.price_history("TEST", frequency=5)
        self.assertEqual(len(client.requests), 1)


if __name__ == "__main__":
    unittest.main()
