"""Validated Nasdaq halt evidence, including causally received end updates."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from threading import RLock
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class OverlayHaltResumption:
    end_et: datetime
    observed_at_utc: datetime


@dataclass(frozen=True, slots=True)
class OverlayHaltInterval:
    start_et: datetime
    end_et: datetime | None
    reason_code: str
    # None identifies older historical snapshots with no first-observed clock.
    first_observed_at_utc: datetime | None = None
    end_observed_at_utc: datetime | None = None
    resumption_updates: tuple[OverlayHaltResumption, ...] = ()

    @property
    def latest_observed_at_utc(self) -> datetime | None:
        receipts = [value for value in (
            self.first_observed_at_utc, self.end_observed_at_utc,
            *(update.observed_at_utc for update in self.resumption_updates),
        ) if value is not None]
        return max(receipts) if receipts else None

    def visible_at(self, cutoff: datetime) -> OverlayHaltInterval | None:
        """Expose only the halt and resumption reports received by this clock."""
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("halt visibility cutoff must include a timezone")
        first = self.first_observed_at_utc or self.start_et
        if first > cutoff:
            return None
        if self.resumption_updates:
            known = tuple(
                update for update in self.resumption_updates
                if update.observed_at_utc <= cutoff
            )
            latest = known[-1] if known else None
            return replace(
                self, end_et=latest.end_et if latest else None,
                end_observed_at_utc=latest.observed_at_utc if latest else None,
                resumption_updates=known,
            )
        if self.end_observed_at_utc is not None and self.end_observed_at_utc > cutoff:
            return replace(self, end_et=None, end_observed_at_utc=None)
        return self


def merge_live_halt_intervals(
    previous: tuple[OverlayHaltInterval, ...],
    incoming: tuple[OverlayHaltInterval, ...],
) -> tuple[OverlayHaltInterval, ...]:
    """Retain received knowledge when a live evidence file is refreshed.

    In particular, legacy fallback receipts may move forward with the latest
    file write. Repeated evidence of an unchanged endpoint cannot make an
    earlier known receipt disappear. Genuine endpoint changes remain ordered.
    """
    before = {item.start_et: item for item in previous}
    after = {item.start_et: item for item in incoming}
    if len(before) != len(previous) or len(after) != len(incoming):
        raise ValueError("live Nasdaq halt evidence contains duplicate episodes")
    if before.keys() - after.keys():
        raise ValueError("live Nasdaq halt evidence lost an existing episode")
    merged: list[OverlayHaltInterval] = []
    for candidate in incoming:
        old = before.get(candidate.start_et)
        if old is None:
            merged.append(candidate)
            continue
        if candidate.first_observed_at_utc != old.first_observed_at_utc:
            raise ValueError("live Nasdaq halt first observation changed")
        if old.end_et is not None and candidate.end_et is None:
            raise ValueError("live Nasdaq halt evidence lost a reported resumption")
        by_receipt: dict[datetime, datetime] = {}
        for interval in (old, candidate):
            updates = interval.resumption_updates
            if not updates and interval.end_et is not None:
                if interval.end_observed_at_utc is None:
                    raise ValueError("live Nasdaq resumption lacks an observation time")
                updates = (OverlayHaltResumption(interval.end_et, interval.end_observed_at_utc),)
            for update in updates:
                known = by_receipt.get(update.observed_at_utc)
                if known is not None and known != update.end_et:
                    raise ValueError("live Nasdaq resumption conflicts at one receipt")
                by_receipt[update.observed_at_utc] = update.end_et
        history: list[OverlayHaltResumption] = []
        for observed, end in sorted(by_receipt.items()):
            if not history or history[-1].end_et != end:
                history.append(OverlayHaltResumption(end, observed))
        latest = history[-1] if history else None
        merged.append(replace(
            candidate, end_et=latest.end_et if latest else None,
            end_observed_at_utc=latest.observed_at_utc if latest else None,
            resumption_updates=tuple(history),
        ))
    return tuple(sorted(merged, key=lambda item: item.start_et))


def load_overlay_halt_intervals(
    path: Path, *, symbol: str, session_date: date
) -> tuple[OverlayHaltInterval, ...]:
    """Load confirmed intervals from the probe's normalized.json."""

    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("Nasdaq halt evidence must be a JSON record list")
    intervals: list[OverlayHaltInterval] = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Nasdaq halt record must be an object")
        if str(record.get("symbol", "")).upper() != symbol.upper():
            continue
        if record.get("halt_date") != session_date.strftime("%m/%d/%Y"):
            continue
        if record.get("retrieval_mode") != "HISTORICAL":
            raise ValueError("Overlay requires historical Nasdaq halt evidence")
        reason = str(record.get("reason_code", "")).strip()
        if not reason:
            raise ValueError("Nasdaq halt reason is missing")
        halt = _time(session_date, record.get("halt_time"))
        resume_text = record.get("resumption_trade_time")
        resume: datetime | None = None
        if resume_text:
            resume_date = record.get("resumption_date")
            if resume_date != session_date.strftime("%m/%d/%Y"):
                raise ValueError("Resumption trade date differs from replay session")
            resume = _time(session_date, resume_text)
            if resume < halt:
                raise ValueError("Resumption trade precedes halt")
        intervals.append(OverlayHaltInterval(halt, resume, reason))
    return tuple(
        sorted(
            set(intervals),
            key=lambda interval: (
                interval.start_et,
                interval.end_et or interval.start_et,
                interval.reason_code,
            ),
        )
    )


def load_daily_nasdaq_halt_events(
    path: Path, *, symbol: str, session_date: date
) -> tuple[OverlayHaltInterval, ...]:
    """Load Nasdaq reports without treating later resumptions as early knowledge.

    Existing daily files recover receipt history from their retained normalized
    captures. If those captures are unavailable, ``last_observed_at`` is a
    conservative bound for when the latest reported endpoint was known. New
    producers preserve endpoint changes directly in each episode.
    """

    episodes = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(episodes, dict):
        raise ValueError("daily Nasdaq episodes must be a JSON object")
    selected = symbol.upper()
    intervals: list[OverlayHaltInterval] = []
    for key, episode in episodes.items():
        if not isinstance(key, str) or not key.startswith(selected + "@"):
            continue
        if not isinstance(episode, dict) or not isinstance(episode.get("record"), dict):
            raise ValueError("daily Nasdaq episode is missing its record")
        record = episode["record"]
        if str(record.get("symbol", "")).upper() != selected:
            raise ValueError("daily Nasdaq episode symbol differs from key")
        keyed_at = _parse_aware(key.split("@", 1)[1], "episode key")
        keyed_date = keyed_at.astimezone(ET).date()
        recorded_date = record.get("halt_date")
        if keyed_date != session_date:
            # A CURRENT feed can still report an episode that began on a
            # previous trading date. Do not present it as today's new halt
            # or block this symbol's otherwise valid quote history.
            # Only ignore it when its key and recorded date agree.
            if recorded_date != keyed_date.strftime("%m/%d/%Y"):
                raise ValueError("daily Nasdaq episode key differs from halt date")
            if keyed_date > session_date:
                raise ValueError("daily Nasdaq episode belongs to a future session")
            continue
        if recorded_date != session_date.strftime("%m/%d/%Y"):
            raise ValueError("daily Nasdaq episode belongs to a different session")
        if record.get("retrieval_mode") not in {"CURRENT", "HISTORICAL"}:
            raise ValueError("daily Nasdaq episode retrieval mode is invalid")
        reason = str(record.get("reason_code", "")).strip()
        if not reason:
            raise ValueError("daily Nasdaq halt reason is missing")
        start = _time(session_date, record.get("halt_time"))
        if abs((keyed_at - start.astimezone(timezone.utc)).total_seconds()) > 0.001:
            raise ValueError("daily Nasdaq episode key differs from halt time")
        first = _parse_aware(episode.get("first_observed_at"), "first_observed_at")
        last = _parse_aware(episode.get("last_observed_at"), "last_observed_at")
        if first < keyed_at or last < first:
            raise ValueError("daily Nasdaq episode observation times are inconsistent")
        if first.astimezone(ET).date() != session_date:
            raise ValueError("daily Nasdaq first observation falls outside session")
        if last.astimezone(ET).date() != session_date:
            raise ValueError("daily Nasdaq last observation falls outside session")
        end = _reported_end(record, session_date, start)
        updates = _daily_resumption_updates(
            Path(path), episode, symbol=selected, start=start, first=first,
            last=last, end=end,
        )
        latest = updates[-1] if updates else None
        intervals.append(OverlayHaltInterval(
            start, latest.end_et if latest else None, reason, first,
            latest.observed_at_utc if latest else None, updates,
        ))
    return tuple(sorted(intervals, key=lambda item: (item.start_et, item.first_observed_at_utc)))


def _reported_end(record: dict, session_date: date, start: datetime) -> datetime | None:
    text = record.get("resumption_trade_time")
    if not text:
        return None
    resume_date = record.get("resumption_date")
    if resume_date not in {session_date.strftime("%m/%d/%Y"), session_date.isoformat()}:
        raise ValueError("daily Nasdaq resumption belongs to another session")
    end = _time(session_date, text)
    if end < start:
        raise ValueError("daily Nasdaq resumption precedes halt")
    return end


def _daily_resumption_updates(
    path: Path, episode: dict, *, symbol: str, start: datetime,
    first: datetime, last: datetime, end: datetime | None,
) -> tuple[OverlayHaltResumption, ...]:
    updates: list[OverlayHaltResumption] = []
    saved = episode.get("resumption_updates", [])
    if not isinstance(saved, list):
        raise ValueError("daily Nasdaq resumption updates must be a list")
    previous_receipt: datetime | None = None
    for value in saved:
        if not isinstance(value, dict):
            raise ValueError("daily Nasdaq resumption update must be an object")
        received = _parse_aware(value.get("observed_at"), "resumption observed_at")
        reported = _reported_end(value, start.date(), start)
        if reported is None:
            raise ValueError("daily Nasdaq resumption update lacks trade time")
        if not first <= received <= last or received.astimezone(ET).date() != start.date():
            raise ValueError("daily Nasdaq resumption observation times are inconsistent")
        if previous_receipt is not None and received < previous_receipt:
            raise ValueError("daily Nasdaq resumption updates are not ordered")
        previous_receipt = received
        updates.append(OverlayHaltResumption(reported, received))

    complete = episode.get("resumption_history_complete", False)
    if not isinstance(complete, bool):
        raise ValueError("daily Nasdaq resumption history completeness must be boolean")
    if not complete:
        captured = _capture_index(str((path.parent / "captures").resolve())).history(
            symbol, start,
        )
        updates.extend(update for update in captured if first <= update.observed_at_utc <= last)

    ordered = sorted(set(updates), key=lambda item: (item.observed_at_utc, item.end_et))
    result: list[OverlayHaltResumption] = []
    for update in ordered:
        if result and update.observed_at_utc == result[-1].observed_at_utc:
            if update.end_et != result[-1].end_et:
                raise ValueError("daily Nasdaq resumption updates conflict at one receipt")
            continue
        if not result or update.end_et != result[-1].end_et:
            result.append(update)
    if complete and end is not None and (not result or result[-1].end_et != end):
        raise ValueError("daily Nasdaq complete resumption history differs from latest record")
    if end is not None and (not result or result[-1].end_et != end):
        # The final record is definitely known by its latest receipt. Do not
        # invent an earlier time when its matching capture has not survived.
        if result and result[-1].observed_at_utc == last:
            raise ValueError("daily Nasdaq latest record conflicts with its capture")
        result.append(OverlayHaltResumption(end, last))
    if end is None and result:
        raise ValueError("daily Nasdaq resumption history differs from latest record")
    return tuple(result)


class _CaptureIndex:
    """Index immutable normalized captures once, then read only new files.

    Directory timestamps avoid a full listing on every live refresh or symbol
    switch. The bounded outer cache retains at most four session directories;
    this index stores only endpoint receipts, rather than all feed payloads.
    """

    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.stamp: tuple[int, int] | None = None
        self.seen: set[str] = set()
        self.histories: dict[tuple[str, datetime], list[OverlayHaltResumption]] = {}
        self.lock = RLock()

    def history(self, symbol: str, start: datetime) -> tuple[OverlayHaltResumption, ...]:
        with self.lock:
            return self._history(symbol, start)

    def _history(self, symbol: str, start: datetime) -> tuple[OverlayHaltResumption, ...]:
        try:
            stat = self.root.stat()
            stamp = (stat.st_mtime_ns, stat.st_ctime_ns)
        except OSError:
            return ()
        if stamp != self.stamp:
            retry = False
            try:
                paths = sorted(self.root.glob("*.json"))
            except OSError:
                return ()
            for path in paths:
                if path.name in self.seen:
                    continue
                read_failed = False
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(payload, dict) or payload.get("mode") not in {"CURRENT", "HISTORICAL"}:
                        continue
                    observed = _parse_aware(payload.get("observed_at"), "capture observed_at")
                    records = payload.get("records")
                    if not isinstance(records, list):
                        continue
                    for record in records:
                        if not isinstance(record, dict) or not record.get("resumption_trade_time"):
                            continue
                        try:
                            day = datetime.strptime(record["halt_date"], "%m/%d/%Y").date()
                            halted = _time(day, record.get("halt_time"))
                            ended = _reported_end(record, day, halted)
                            name = str(record.get("symbol", "")).strip().upper()
                            if not name or observed < halted or observed.astimezone(ET).date() != day:
                                continue
                        except (KeyError, TypeError, ValueError):
                            continue
                        if ended is not None:
                            key = (name, halted)
                            history = self.histories.setdefault(key, [])
                            history.append(OverlayHaltResumption(ended, observed))
                except OSError:
                    # Windows may briefly deny a read during publication. Do
                    # not permanently discard that receipt or cache the miss.
                    read_failed = retry = True
                    continue
                except (TypeError, ValueError):
                    continue
                finally:
                    # Producer captures are immutable and atomically published.
                    if not read_failed:
                        self.seen.add(path.name)
            # Keep repeated receipts here: a temporarily locked older capture
            # can be recovered on the next refresh. Collapsing equal endpoints
            # before that recovery could erase a genuine intervening revision.
            # The public loader orders and coalesces the full received history.
            self.stamp = None if retry else stamp
        return tuple(self.histories.get((symbol, start), ()))


@lru_cache(maxsize=4)
def _capture_index(root: str) -> _CaptureIndex:
    return _CaptureIndex(root)


def _parse_aware(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} is not an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _time(session_date: date, value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Nasdaq halt time is missing")
    return datetime.fromisoformat(f"{session_date.isoformat()}T{''.join(value.split())}").replace(tzinfo=ET)
