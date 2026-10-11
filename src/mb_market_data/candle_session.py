"""Follow a supervised candle session across preserved, separately named runs."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from mb_market_data.candle_receipts import (
    CandleReceiptError, CandleReceiptProjection, CandleReceiptReader, CapturedCandleVersion,
    _instant,
)


class CandleSessionReader:
    def __init__(self, directory):
        self.directory = Path(directory).resolve(strict=True)
        self._plan_path = self.directory / "session_plan.json"
        body = self._plan_path.read_bytes()
        self._plan_hash = hashlib.sha256(body).hexdigest()
        plan = json.loads(body)
        self.symbol = plan["symbol"]
        self.session_date = date.fromisoformat(plan["session_date"])
        self._readers = {}
        self.caught_up = False

    @property
    def receipt_count(self):
        return sum(reader.receipt_count for reader in self._readers.values())

    @property
    def availability_times(self):
        return tuple(sorted({t for reader in self._readers.values() for t in reader.availability_times}))

    def refresh(self, limit=32):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be positive")
        if hashlib.sha256(self._plan_path.read_bytes()).hexdigest() != self._plan_hash:
            raise CandleReceiptError("Supervised candle session plan changed")
        paths = sorted(self.directory.glob("run_*/request_plan.json"))
        directories = {p.parent.name: p.parent for p in paths}
        if not self._readers.keys() <= directories.keys():
            raise CandleReceiptError("Previously discovered candle run disappeared")
        # Each run stages its own page. Already validated observations remain
        # available if another run fails validation.
        remaining = limit
        for name, folder in directories.items():
            if folder.is_symlink() or folder.resolve().parent != self.directory:
                raise CandleReceiptError("Candle run leaves the session directory")
            if name not in self._readers:
                reader = CandleReceiptReader(folder)
                if (reader.symbol, reader.session_date) != (self.symbol, self.session_date):
                    raise CandleReceiptError("Candle run symbol/session differs")
                self._readers[name] = reader
            reader = self._readers[name]
            if remaining > 0:
                before = reader.receipt_count
                reader.refresh(limit=remaining)
                remaining -= reader.receipt_count - before
        self.caught_up = bool(self._readers) and all(r.caught_up for r in self._readers.values())

    def project(self, cutoff):
        cutoff = _instant(cutoff.isoformat(), "cutoff")
        if cutoff.astimezone(ZoneInfo("America/New_York")).date() != self.session_date:
            raise ValueError("cutoff must be on the candle session date")
        # Reuse the established availability-time projector across all runs.
        # Old versions remain known before a later run's receipt, including
        # when the latter retrieves candles for an earlier market interval.
        observations = []
        for name, reader in self._readers.items():
            for receipt in reader.validated_receipts:
                if receipt.received_at_utc <= cutoff:
                    observations.append((receipt.received_at_utc, name, receipt.sample_number, receipt))
        observations.sort(key=lambda item: item[:3])
        latest, versions, previous = {}, {}, {}
        for received, name, number, receipt in observations:
            for candle in receipt.candles:
                key = candle.start_et
                if previous.get(key) != candle:
                    versions[key] = versions.get(key, 0) + 1
                previous[key] = candle
                latest[key] = CapturedCandleVersion(candle, received, number, receipt.raw_sha256, versions[key], name)
        ordered = tuple(latest[key] for key in sorted(latest))
        return CandleReceiptProjection(self.symbol, self.session_date, cutoff,
            tuple(v for v in ordered if v.interval_closed_at_receipt),
            tuple(v for v in ordered if not v.interval_closed_at_receipt),
            len(observations), sum(item[3].outcome != "OK" for item in observations),
            observations[-1][0] if observations else None)
