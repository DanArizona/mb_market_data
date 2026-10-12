"""Read immutable price-history receipts without API calls or journal writes.

The source is the separate live_candle_capture_v1 directory. Every successful
response retains its raw body, hash and receipt time. Projection chooses a
version known at the inspection cutoff; interval closure alone cannot promote
an unfinished observation into a closed-interval candle.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path

from mb_market_data.observation_overlay import (
    ET, ObservationOverlayData, ObservationOverlayError, OverlayCandle,
)

UTC = timezone.utc
_RECEIPT_NAME = re.compile(r"sample_(\d{4,})\.receipt\.json\Z")


class CandleReceiptError(ObservationOverlayError):
    """The captured candle evidence is inconsistent or has changed."""


def _instant(value, name):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result.astimezone(UTC)
    except (TypeError, ValueError) as exc:
        raise CandleReceiptError(f"{name} must be an aware ISO timestamp") from exc


def _digest(body):
    return hashlib.sha256(body).hexdigest()


def _fingerprint(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


@dataclass(frozen=True, slots=True)
class CapturedCandleVersion:
    candle: OverlayCandle
    received_at_utc: datetime
    sample_number: int
    raw_sha256: str
    observed_version: int
    capture_run_id: str | None = None

    @property
    def interval_closed_at_receipt(self):
        return self.received_at_utc >= self.candle.available_at_utc


@dataclass(frozen=True, slots=True)
class CandleReceiptProjection:
    symbol: str
    session_date: date
    cutoff: datetime
    closed: tuple[CapturedCandleVersion, ...]
    pending: tuple[CapturedCandleVersion, ...]
    receipt_count: int
    error_count: int
    last_received_at_utc: datetime | None

    def attach(self, overlay: ObservationOverlayData):
        if (overlay.symbol, overlay.session_date, overlay.replay_time_utc) != (
                self.symbol, self.session_date, self.cutoff):
            raise CandleReceiptError("Candle projection and Overlay identity/cutoff differ")
        if overlay.cache is not None:
            raise CandleReceiptError("Captured receipts cannot be mixed with a retrospective cache")
        return replace(overlay, candles=tuple(v.candle for v in self.closed),
                       captured_candle_versions=self.closed, pending_candle_versions=self.pending,
                       candle_capture=True, candle_receipt_count=self.receipt_count,
                       candle_error_count=self.error_count, candle_last_received_at_utc=self.last_received_at_utc)


@dataclass(frozen=True, slots=True)
class _Receipt:
    sample_number: int
    received_at_utc: datetime
    candles: tuple[OverlayCandle, ...]
    raw_sha256: str
    outcome: str


@dataclass(frozen=True, slots=True)
class ReceiptSummary:
    sample_number: int
    received_at_utc: datetime
    outcome: str


class CandleReceiptReader:
    """Follow complete receipt files in bounded pages; preserve validated state.

    The writer must use new sample filenames and retain existing files. Raw
    files without a receipt are not committed input. Prior-file stat changes
    trigger content/hash revalidation; this is an append-only reader guard,
    not protection against a malicious writer restoring all file metadata.
    """

    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve(strict=True)
        self._plan_path = self.directory / "request_plan.json"
        body = self._plan_path.read_bytes()
        plan = json.loads(body)
        try:
            self.symbol = plan["symbol"].strip().upper()
            self.session_date = date.fromisoformat(plan["session_date"])
            self.request_start = _instant(plan["request_start_et"], "request_start_et")
            self.request_end = _instant(plan["request_end_et"], "request_end_et")
            if (not self.symbol or plan["frequency_minutes"] != 5
                    or isinstance(plan["frequency_minutes"], bool)
                    or not isinstance(plan["extended_hours"], bool)
                    or self.request_start >= self.request_end
                    or any(t.astimezone(ET).date() != self.session_date for t in (self.request_start, self.request_end))):
                raise ValueError()
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise CandleReceiptError("Invalid five-minute capture plan") from exc
        self._plan_hash = _digest(body)
        self._receipts: dict[int, _Receipt] = {}
        self._anchors: dict[str, tuple] = {}
        self.caught_up = False

    def _raw_path(self, name):
        if not isinstance(name, str) or Path(name).name != name or "/" in name or "\\" in name:
            raise CandleReceiptError("Response path must be one filename")
        path = (self.directory / name).resolve(strict=True)
        if path.parent != self.directory:
            raise CandleReceiptError("Response path leaves the capture directory")
        return path

    @property
    def receipt_summaries(self):
        """Progress from already validated committed evidence only."""
        return tuple(ReceiptSummary(r.sample_number, r.received_at_utc, r.outcome)
                     for r in sorted(self._receipts.values(), key=lambda r: r.sample_number))

    @property
    def validated_receipts(self):
        """Immutable observations for a session that preserves several runs."""
        return tuple(sorted(self._receipts.values(), key=lambda r: r.sample_number))

    def _read(self, path, number):
        receipt_body = path.read_bytes()
        item = json.loads(receipt_body)
        if not isinstance(item, dict):
            raise CandleReceiptError("Receipt must be a JSON object")
        if item.get("sample_number") != number or isinstance(item.get("sample_number"), bool):
            raise CandleReceiptError("Receipt number differs from its filename")
        received = _instant(item.get("received_at_utc"), "received_at_utc")
        sent = _instant(item.get("dispatched_at_utc"), "dispatched_at_utc")
        if received < sent or received.astimezone(ET).date() != self.session_date:
            raise CandleReceiptError("Invalid receipt timing/session")
        outcome = item.get("outcome")
        if outcome not in {"OK", "NO_CANDLES", "HTTP_ERROR", "PAYLOAD_ERROR", "REQUEST_ERROR"}:
            raise CandleReceiptError("Unknown receipt outcome")
        raw, raw_path, raw_hash = None, None, ""
        if "raw_response_file" in item:
            raw_path = self._raw_path(item["raw_response_file"])
            raw = raw_path.read_bytes()
            raw_hash = _digest(raw)
            if raw_hash != item.get("raw_sha256"):
                raise CandleReceiptError("Raw response hash does not match its receipt")
        candles = []
        if outcome == "OK":
            status = item.get("http_status")
            if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status < 300 or raw is None:
                raise CandleReceiptError("Successful receipt lacks a successful HTTP response")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get("candles"), list):
                raise CandleReceiptError("Response has no candle list")
            if payload.get("symbol", self.symbol) != self.symbol:
                raise CandleReceiptError("Response symbol differs from capture plan")
            seen = set()
            for value in payload["candles"]:
                epoch = value.get("datetime") if isinstance(value, dict) else None
                if isinstance(epoch, bool) or not isinstance(epoch, (int, float)) or not math.isfinite(epoch):
                    raise CandleReceiptError("Invalid candle timestamp")
                try:
                    when = datetime.fromtimestamp(epoch / 1000, UTC)
                except (OverflowError, OSError, ValueError) as exc:
                    raise CandleReceiptError("Candle timestamp is out of range") from exc
                if not self.request_start <= when < self.request_end:
                    continue
                if when in seen or when > received:
                    raise CandleReceiptError("Duplicate or future candle timestamp")
                seen.add(when)
                volume = value.get("volume")
                if isinstance(volume, float) and math.isfinite(volume) and volume.is_integer():
                    volume = int(volume)
                try:
                    candles.append(OverlayCandle(self.symbol, when, value.get("open"), value.get("high"),
                                                 value.get("low"), value.get("close"), volume))
                except (ValueError, TypeError) as exc:
                    raise CandleReceiptError("Invalid five-minute OHLCV candle") from exc
            if not candles:
                raise CandleReceiptError("Successful receipt contains no requested-session candles")
        anchor = (_digest(receipt_body), _fingerprint(path), raw_path, raw_hash,
                  _fingerprint(raw_path) if raw_path else None)
        return _Receipt(number, received, tuple(sorted(candles, key=lambda c: c.start_et)), raw_hash, outcome), anchor

    def refresh(self, *, limit=32):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        if _digest(self._plan_path.read_bytes()) != self._plan_hash:
            raise CandleReceiptError("Capture plan changed")
        names = {p.name: p for p in self.directory.glob("sample_*.receipt.json") if _RECEIPT_NAME.fullmatch(p.name)}
        anchors = dict(self._anchors)
        for name, old in self._anchors.items():
            if name not in names:
                raise CandleReceiptError("Previously discovered receipt was removed")
            _, stat, raw_path, raw_hash, raw_stat = old
            if _fingerprint(names[name]) != stat or (raw_path and _fingerprint(raw_path) != raw_stat):
                receipt, anchor = self._read(names[name], int(_RECEIPT_NAME.fullmatch(name)[1]))
                if anchor[0] != old[0] or anchor[3] != raw_hash or receipt != self._receipts[receipt.sample_number]:
                    raise CandleReceiptError("Previously discovered candle evidence changed")
                anchors[name] = anchor
        staged = dict(self._receipts)
        unseen = sorted((n for n in names if n not in anchors), key=lambda n: int(_RECEIPT_NAME.fullmatch(n)[1]))
        for name in unseen[:limit]:
            number = int(_RECEIPT_NAME.fullmatch(name)[1])
            if number in staged:
                raise CandleReceiptError("Sample number has multiple filenames")
            receipt, anchor = self._read(names[name], number)
            staged[number], anchors[name] = receipt, anchor
        ordered = sorted(staged.values(), key=lambda r: r.sample_number)
        if any(a.received_at_utc > b.received_at_utc for a, b in zip(ordered, ordered[1:])):
            raise CandleReceiptError("Receipt times regress within one capture")
        self._receipts, self._anchors = staged, anchors
        self.caught_up = len(unseen) <= limit

    @property
    def availability_times(self):
        return tuple(sorted({r.received_at_utc for r in self._receipts.values()}))

    @property
    def receipt_count(self):
        return len(self._receipts)

    def project(self, cutoff: datetime):
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("cutoff must be timezone-aware")
        cutoff = cutoff.astimezone(UTC)
        if cutoff.astimezone(ET).date() != self.session_date:
            raise ValueError("cutoff must be on the capture session")
        visible = sorted((r for r in self._receipts.values() if r.received_at_utc <= cutoff),
                         key=lambda r: (r.received_at_utc, r.sample_number))
        latest, versions, previous = {}, {}, {}
        for receipt in visible:
            for candle in receipt.candles:
                key = candle.start_et
                if previous.get(key) != candle:
                    versions[key] = versions.get(key, 0) + 1
                previous[key] = candle
                latest[key] = CapturedCandleVersion(candle, receipt.received_at_utc, receipt.sample_number,
                                                   receipt.raw_sha256, versions[key])
        all_versions = [v for _, v in sorted(latest.items())]
        return CandleReceiptProjection(self.symbol, self.session_date, cutoff,
            tuple(v for v in all_versions if v.interval_closed_at_receipt),
            tuple(v for v in all_versions if not v.interval_closed_at_receipt), len(visible),
            sum(r.outcome != "OK" for r in visible), visible[-1].received_at_utc if visible else None)
