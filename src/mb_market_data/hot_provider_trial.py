"""Bounded, isolated Hot transport trial. No production cadence is selected here."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
import sys
from pathlib import Path
from threading import Event

from mb_market_data.quote_event_state import QuoteEventStateProjector
from mb_market_data.quote_journal_audit import audit_quote_journal
from mb_market_data.quote_journal_replay import QuoteJournalReplayReader
from mb_market_data.quote_observation_store import QuoteObservationStore
from mb_market_data.sampling_acquisition import MARKER, AcquisitionSettings, IsolatedSamplingPoller, aware
from mb_market_data.sampling_membership import SamplingChannel, SamplingHierarchyRevision
from mb_market_data.watchlist_polling import ET, PollWindow

CONTRACT = "isolated-hot-provider-trial-v1"


def software_versions():
    result = {"python": sys.version.split()[0]}
    for name in ("mb-tools", "schwabdev"):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = "not installed"
    return result


def utc_now():
    return datetime.now(timezone.utc)


def json_value(value):
    if is_dataclass(value):
        return {f.name: json_value(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(json_value(value), indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


@dataclass(frozen=True)
class TrialSettings:
    symbols: tuple[str, ...]
    intervals: tuple[int, ...]
    phase_seconds: int
    quote_call_budget: int
    request_timeout_seconds: int
    source: str

    def __post_init__(self):
        if not isinstance(self.symbols, tuple) or not 1 <= len(self.symbols) <= 80 or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("Provide 1..80 distinct symbols")
        if any(not isinstance(s, str) or re.fullmatch(r"[A-Z][A-Z0-9./-]{0,14}", s) is None for s in self.symbols):
            raise ValueError("Symbols must be explicit uppercase quote symbols")
        if not isinstance(self.intervals, tuple) or not self.intervals or len(self.intervals) > 3 or len(set(self.intervals)) != len(self.intervals) or any(type(i) is not int or i not in (2, 3, 4) for i in self.intervals):
            raise ValueError("Use distinct explicit intervals from 2, 3, 4 seconds")
        if type(self.phase_seconds) is not int or not 4 <= self.phase_seconds <= 600 or any(self.phase_seconds % i for i in self.intervals):
            raise ValueError("Phase duration must be 4..600 seconds and divisible by every interval")
        if type(self.quote_call_budget) is not int or not self.planned_calls <= self.quote_call_budget <= 600:
            raise ValueError("Quote-call budget must cover the plan and be at most 600")
        if type(self.request_timeout_seconds) is not int or not 1 <= self.request_timeout_seconds <= 10:
            raise ValueError("Request timeout must be an integer in 1..10 seconds")
        if self.source not in ("synthetic", "schwab"):
            raise ValueError("Explicit synthetic or schwab source required")

    @property
    def planned_calls(self):
        return sum(self.phase_seconds // interval for interval in self.intervals)

    def required_through(self, now):
        # Conservative allowance for phase setup and alignment; checked again after authentication.
        return aware(now) + timedelta(seconds=len(self.intervals) * (self.phase_seconds + 15))

    def check_session(self, now):
        if self.required_through(now).astimezone(ET).date() != now.astimezone(ET).date():
            raise ValueError("Trial is too near ET midnight; retry after the date changes")


class RejectInteractiveAuthorization:
    def __init__(self):
        self.requested = False

    def __call__(self, _url):
        self.requested = True
        return ""


def prepare_schwab_client(ecfg_path, password, settings, *, clock=utc_now):
    """Existing secure config/token interfaces; browser OAuth is a separate command."""
    from mb_tools.schwab_secure import (
        DEFAULT_POLLING_REFRESH_MARGIN, load_secure_schwab_config,
        make_client_from_config, read_schwab_token_status,
    )
    settings.check_session(clock())
    config = load_secure_schwab_config(Path(ecfg_path), password)
    status = read_schwab_token_status(config)
    status.require_refresh_valid_through(settings.required_through(clock()) + DEFAULT_POLLING_REFRESH_MARGIN)
    rejection = RejectInteractiveAuthorization()
    client = make_client_from_config(config, timeout=settings.request_timeout_seconds, call_on_auth=rejection)
    try:
        if rejection.requested:
            raise RuntimeError("Authorization required; run mb-schwab-auth separately")
        settings.check_session(clock())
        status.require_refresh_valid_through(settings.required_through(clock()) + DEFAULT_POLLING_REFRESH_MARGIN)
    except BaseException:
        client.close()
        raise
    return client, rejection


class SafeResponse:
    """Keep transport errors out of persisted exception text and provider error bodies."""
    def __init__(self, response, metric):
        self.response = response
        self.metric = metric
        self.status_code = int(response.status_code)
        self.ok = bool(response.ok)
        self.text = "Provider HTTP error" if not self.ok else ""

    def json(self):
        if not self.ok:
            return {}  # HTTP error bodies may contain request details; only status is retained.
        try:
            return self.response.json()
        except Exception as exc:
            self.metric["decode_error_type"] = type(exc).__name__
            raise RuntimeError("Quote JSON decode failed (" + type(exc).__name__ + ")") from None


class MeasuredClient:
    def __init__(self, client, *, budget, clock=utc_now, timer=time.perf_counter, auth_rejection=None):
        self.client, self.budget, self.clock, self.timer = client, budget, clock, timer
        self.auth_rejection = auth_rejection
        self.calls = []
        self.deadline = None
        self.halt_reason = None

    def quotes(self, symbols, *, fields):
        if self.halt_reason or (self.auth_rejection and self.auth_rejection.requested):
            self.halt_reason = self.halt_reason or "authorization_required"
            raise RuntimeError("Trial stopped: " + self.halt_reason)
        if len(self.calls) >= self.budget:
            self.halt_reason = "quote_call_budget_exhausted"
            raise RuntimeError("Trial quote-call budget exhausted")
        if self.deadline is None or self.clock() >= self.deadline:
            raise RuntimeError("Phase request deadline reached")
        metric = {"started_at_utc": self.clock().isoformat(), "symbol_count": len(symbols)}
        self.calls.append(metric)
        started = self.timer()
        try:
            response = self.client.quotes(symbols, fields=fields)
            metric["status_code"] = int(response.status_code)
            if response.status_code in (401, 403) or (self.auth_rejection and self.auth_rejection.requested):
                self.halt_reason = "authorization_required"
            return SafeResponse(response, metric)
        except Exception as exc:
            metric["exception_type"] = type(exc).__name__
            if self.auth_rejection and self.auth_rejection.requested:
                self.halt_reason = "authorization_required"
            raise RuntimeError("Quote transport failed (" + type(exc).__name__ + ")") from None
        finally:
            metric["returned_at_utc"] = self.clock().isoformat()
            metric["elapsed_seconds"] = self.timer() - started


class DeadlineStop:
    """Finish retry loops at the phase boundary, including persistent SQLite contention."""
    def __init__(self, deadline, external, *, clock=utc_now, wait=None):
        self.deadline, self.external, self.clock = deadline, external, clock
        self.wait_external = wait or external.wait

    def is_set(self):
        return self.external.is_set() or self.clock() >= self.deadline

    def wait(self, seconds):
        if not self.is_set():
            self.wait_external(min(max(0, seconds), max(0, (self.deadline - self.clock()).total_seconds())))
        return self.is_set()


def timing(values):
    values = sorted(values)
    if not values:
        return {"count": 0, "mean_ms": None, "p95_ms": None, "max_ms": None}
    return {"count": len(values), "mean_ms": round(1000 * sum(values) / len(values), 3),
            "p95_ms": round(1000 * values[math.ceil(.95 * len(values)) - 1], 3), "max_ms": round(1000 * values[-1], 3)}


def growth_timings(outcomes):
    acquired = [o for o in outcomes if "cycle_seconds" in o]
    if not acquired:
        return {}
    size = math.ceil(len(acquired) / 3)
    return {label: {key: timing([o[key] for o in group if key in o]) for key in
                   ("cycle_seconds", "verification_seconds", "journal_seconds")}
            for label, group in (("first_third", acquired[:size]),
                                 ("middle_third", acquired[size:2*size]),
                                 ("last_third", acquired[2*size:]))}


def replay_digest(path):
    digest = hashlib.sha256()
    projector = QuoteEventStateProjector()
    for event in QuoteJournalReplayReader(path).events():
        projector.apply(event)
        digest.update(json.dumps(json_value(event), sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
        digest.update(b"\n")
    state = projector.snapshot()
    return {"sequence_sha256": digest.hexdigest(), "events": state.event_count,
            "acquisitions": state.acquisition_count, "observations": state.observation_count}


def validate_output_path(path):
    path = Path(path).resolve()
    if any(p.lower().startswith("quote_observation_journal") for p in path.parts):
        raise ValueError("Production journal paths are excluded")
    if path.exists():
        raise ValueError("Trial output must be a new directory")
    return path


def phase_start(now, interval):
    # Whole-second grid in ET, with at least two seconds for phase initialization.
    value = (aware(now) + timedelta(seconds=2)).replace(microsecond=0)
    while (value - now).total_seconds() < 2 or value.second % interval:
        value += timedelta(seconds=1)
    return value


def run_trial(path, settings, client, *, auth_rejection=None, clock=utc_now, wait=None, stop=None, on_status=print, client_preparation_seconds=None, clock_mode="actual_wall_clock"):
    """One fresh journal per phase. Client ownership/close belongs to the caller."""
    if clock_mode not in ("actual_wall_clock", "logical_offline"):
        raise ValueError("Explicit supported clock mode required")
    settings.check_session(clock())
    path = validate_output_path(path)
    path.mkdir(parents=True, exist_ok=False)
    stop = stop or Event()
    measured = MeasuredClient(client, budget=settings.quote_call_budget, clock=clock, auth_rejection=auth_rejection)
    report = {"contract": CONTRACT, "clock_mode": clock_mode, "settings": json_value(settings), "started_at_utc": clock().isoformat(),
              "clock_context_et": clock().astimezone(ET).isoformat(),
              "weekday_et": clock().astimezone(ET).strftime("%A"), "exchange_calendar_checked": False,
              "phases": [], "interrupted": False, "error_type": None,
              "software_versions": software_versions(), "client_preparation_seconds": client_preparation_seconds,
              "market_data_baseline_commit": "a082f544112643b4e5f459d15c67f60d0a1ef83d",
              "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in
                  (Path(__file__), Path(__file__).with_name("sampling_acquisition.py"))},
              "limitations": ["Hot-only bounded-roster trial; no combined channel capacity claim",
                              "Request timeout is not a hard process-runtime limit",
                              "Quote/trade timestamps may be stale outside trading hours",
                              "Quote-client calls counted; SDK authentication HTTP calls are outside this trial counter and budget"]}
    write_json(path / "manifest.json", report)
    try:
        for number, interval in enumerate(settings.intervals, 1):
            if stop.is_set() or measured.halt_reason:
                break
            settings.check_session(clock())
            folder = path / f"phase_{number:02d}_{interval}s"
            folder.mkdir()
            start = phase_start(clock(), interval)
            end = start + timedelta(seconds=settings.phase_seconds)
            measured.deadline = end
            window = PollWindow(start, end)
            journal = QuoteObservationStore(folder / "quotes.sqlite3", session_date=window.session_date, schema_version=2)
            journal.initialize()
            journal.record_membership_revision(SamplingHierarchyRevision(
                session_date=window.session_date, revision=0, effective_at=start,
                uni_symbols=settings.symbols, focus_symbols=settings.symbols, hot_symbols=settings.symbols,
                source=CONTRACT, metadata={MARKER: True, "trial_contract": CONTRACT,
                    "source": settings.source, "clock_mode": clock_mode, "experimental": True, "fixed_roster_transport_trial": True}))
            acquisition_settings = AcquisitionSettings(SamplingChannel.HOT, tuple(range(0, 60, interval)), window,
                400, "all", CONTRACT + ":" + settings.source, .75, .05)
            poller = IsolatedSamplingPoller(journal, settings=acquisition_settings, client=measured, clock=clock)
            poller.initialize()
            outcomes = []
            call_offset = len(measured.calls)
            phase = {"interval_seconds": interval, "start_at_utc": start.isoformat(), "end_at_utc": end.isoformat(),
                     "planned_slots": settings.phase_seconds // interval, "journal": str(journal.database_path),
                     "outcomes": outcomes}
            report["phases"].append(phase)
            on_status(f"Phase {number}/{len(settings.intervals)}: {interval}s interval; {phase['planned_slots']} slots; starts {start.astimezone(ET):%H:%M:%S} ET")
            def status(value):
                # Avoid publishing arbitrary exception messages from dependencies.
                safe = {k: v for k, v in value.items() if k != "error"}
                outcomes.append(safe)
                if measured.halt_reason:
                    stop.set()
                if value.get("status") != "retrying":
                    on_status(f"  {value.get('slot_id', '')}: {value['status']}")
            try:
                phase["loop_result"] = poller.run_loop(DeadlineStop(end, stop, clock=clock, wait=wait), on_status=status)
            except KeyboardInterrupt:
                report["interrupted"] = True
                stop.set()
            except Exception as exc:
                report["error_type"] = type(exc).__name__
                stop.set()
            finally:
                # Current cutoff plus full planned-window audit distinguish stopped work from complete coverage.
                current_audit = audit_quote_journal(journal.database_path, audited_at=clock())
                full_audit = audit_quote_journal(journal.database_path, audited_at=end)
                hot = next(c for c in full_audit.channels if c.channel == "hot")
                first, second = replay_digest(journal.database_path), replay_digest(journal.database_path)
                state = poller.snapshot()
                calls = measured.calls[call_offset:]
                phase.update({"finished_at_utc": clock().isoformat(), "calls": calls,
                    "request_latency": timing([c["elapsed_seconds"] for c in calls]),
                    "timings": {key: timing([o[key] for o in outcomes if key in o]) for key in
                        ("collector_seconds", "journal_seconds", "cycle_seconds", "verification_seconds", "dispatch_lateness_seconds")},
                    "growth_by_completed_acquisitions": growth_timings(outcomes),
                    "verification_diagnostics": {"reads": poller.verification_reads,
                        "total_seconds_including_recovery_and_final_snapshot": poller.verification_seconds,
                        "normalization_seconds": poller.normalization_seconds,
                        "normalization_cache_hits": poller.normalization_cache_hits,
                        "normalization_cache_misses": poller.normalization_cache_misses,
                        "cache_acquisitions": len(poller._normalization_cache), "cache_limit": 1024},
                    "cycles_over_interval": sum(o.get("cycle_seconds", 0) > interval for o in outcomes),
                    "audit_at_stop_passed": current_audit.passed, "full_window_audit_passed": full_audit.passed,
                    "full_window_findings": json_value(full_audit.findings), "channel_audit": json_value(hot),
                    "replay": first, "repeated_replay_identical": first == second,
                    "pending_dispatch": state["pending"] is not None,
                    "sidecar_head_sha256": state["head_sha256"]})
                write_json(folder / "report.json", phase)
                write_json(path / "manifest.json", report)
                on_status(f"Phase {number} finished: {hot.acquisition_count}/{phase['planned_slots']} acquisitions; missing={len(hot.missing_slot_ids)}; statuses={dict(hot.status_counts)}")
    except KeyboardInterrupt:
        report["interrupted"] = True
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    finally:
        report["halt_reason"] = measured.halt_reason
        report["quote_client_calls"] = len(measured.calls)
        report["finished_at_utc"] = clock().isoformat()
        report["complete_coverage"] = (len(report["phases"]) == len(settings.intervals) and all(
            p.get("full_window_audit_passed") and p.get("repeated_replay_identical") and not p.get("pending_dispatch") for p in report["phases"]))
        report["clean_transport"] = all(
            p.get("channel_audit", {}).get("status_counts", {}).get("quote", 0) == p.get("planned_slots", 0) * len(settings.symbols)
            for p in report["phases"]) and report["complete_coverage"]
        report["passed"] = report["complete_coverage"] and report["clean_transport"] and not report["interrupted"] and not report["error_type"] and not report["halt_reason"]
        write_json(path / "manifest.json", report)
        lines = ["ISOLATED HOT PROVIDER TIMING TRIAL", f"Source: {settings.source}; symbols: {' '.join(settings.symbols)}",
                 f"Quote-client calls: {len(measured.calls)}/{settings.quote_call_budget}",
                 f"Full configured coverage: {report['complete_coverage']}; all requested rows are quotes: {report['clean_transport']}",
                 f"Interrupted: {report['interrupted']}; error type: {report['error_type']}; stop reason: {report['halt_reason']}"]
        for p in report["phases"]:
            lines += [f"{p['interval_seconds']}s phase: {p.get('channel_audit', {}).get('acquisition_count', 0)}/{p['planned_slots']} acquisitions",
                      "  Request round trip: " + str(p.get("request_latency")), "  Active cycle: " + str(p.get("timings", {}).get("cycle_seconds")),
                      "  Collector: " + str(p.get("timings", {}).get("collector_seconds")),
                      "  Journal: " + str(p.get("timings", {}).get("journal_seconds")),
                      "  Dispatch lateness: " + str(p.get("timings", {}).get("dispatch_lateness_seconds")),
                      "  Verification within cycle: " + str(p.get("timings", {}).get("verification_seconds")),
                      "  Cycle growth (first/middle/last thirds): " + str({k: v["cycle_seconds"] for k, v in p.get("growth_by_completed_acquisitions", {}).items()})]
        lines += report["limitations"] + ["Reports: " + str(path)]
        (path / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


class SyntheticResponse:
    status_code, ok = 200, True
    def __init__(self, symbols, number):
        self.payload = {s: {"assetMainType": "EQUITY", "realtime": False,
                           "quote": {"lastPrice": 100.0 + number, "totalVolume": number,
                                     "securityStatus": "Synthetic offline timing trial"}} for s in symbols}
    def json(self):
        return self.payload


class SyntheticClient:
    def __init__(self):
        self.count = 0
    def quotes(self, symbols, *, fields):
        self.count += 1
        return SyntheticResponse(symbols, self.count)
    def close(self):
        pass
