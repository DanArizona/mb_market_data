"""Compare a frozen batched-volume proxy with the unchanged candle OV selector.

Caller owns one authenticated client. This module never prompts, closes that
client, publishes membership, or substitutes the proxy for the reference r1.
"""
from __future__ import annotations

import csv
import json
import math
import time
from datetime import datetime, time as day_time, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from .api_overnight_volume import (
    acquire_api_overnight_volume, write_api_overnight_volume_artifacts,
)
from .opening_hierarchy import load_opening_proposal
from .schwab_quotes import fetch_quotes_batched

ET = ZoneInfo('America/New_York')


def write_json(path: Path, value: Any) -> None:
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write('\n')


def utc_text(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def volume_value(value: Any) -> int | None:
    # Do not turn absent/invalid values into zero; real zero remains eligible.
    if type(value) is int and value >= 0:
        return value
    if type(value) is float and math.isfinite(value) and value >= 0 and value.is_integer():
        return int(value)
    return None


def rank_values(values: Mapping[str, int | None]) -> tuple[str, ...]:
    return tuple(sorted((s for s, v in values.items() if v is not None),
                        key=lambda s: (-values[s], s)))


def wait_until(at: datetime, *, clock: Callable[[], datetime],
               sleep: Callable[[float], None]) -> None:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError('An offset-aware snapshot time is required')
    while True:
        now = clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('Clock must be offset-aware')
        remaining = (at - now).total_seconds()
        if remaining <= 0:
            return
        sleep(min(remaining, 1.0))


def build_reference_focus(manifest: Path, opening: Path, output: Path,
                          limit: int, generated_at: datetime) -> dict:
    # These are the accepted production functions, not a reimplementation of OV.
    from schwab_watchlists.api_ov_focus_production import (
        load_api_ov_evidence, select_api_ov_symbols, write_api_ov_focus_artifacts,
    )
    from uuid import uuid4
    evidence, revision = load_api_ov_evidence(
        manifest, opening_proposal_path=opening)
    selection = select_api_ov_symbols(evidence, limit=limit)
    artifacts = write_api_ov_focus_artifacts(
        output, opening_proposal_path=opening, evidence=evidence,
        opening=revision, selection=selection, requested_limit=limit,
        generated_at=generated_at, decision_id='voltest-reference-' + uuid4().hex)
    return {'symbols': list(selection.selected_symbols),
            'proposal': str(artifacts.proposal), 'manifest': str(artifacts.manifest)}


def run_comparison(client: Any, *, opening_path: Path, output_dir: Path,
                   snapshot_at: datetime, limit: int = 40,
                   request_interval: float = 0.5, max_attempts: int = 3,
                   clock: Callable[[], datetime] = lambda: datetime.now(ET),
                   sleep: Callable[[float], None] = time.sleep,
                   monotonic: Callable[[], float] = time.monotonic,
                   focus_builder: Callable = build_reference_focus) -> dict:
    """Freeze quote batches first, then run accepted OV with the SAME client."""
    opening_path = Path(opening_path).resolve()
    root = Path(output_dir).resolve()
    opening = load_opening_proposal(opening_path)
    if opening.revision != 0 or opening.focus_symbols or opening.hot_symbols:
        raise ValueError('Supply the opening r0 with empty Focus and Hot')
    if not opening.uni_symbols:
        raise ValueError('Opening Uni must not be empty')
    if type(limit) is not int or limit < 1:
        raise ValueError('Positive integer selection limit required')
    if request_interval < 0 or type(max_attempts) is not int or max_attempts < 1:
        raise ValueError('Invalid reference request pacing or attempts')
    if snapshot_at.tzinfo is None or snapshot_at.utcoffset() is None:
        raise ValueError('Snapshot time must be offset-aware')
    scheduled = snapshot_at.astimezone(ET)
    if scheduled.date() != opening.session_date or not (
            day_time(9, 0) <= scheduled.time() < day_time(9, 30)):
        raise ValueError('Snapshot must be on the r0 date, from 09:00 to before 09:30 ET')
    now = clock().astimezone(ET)
    if now.date() != opening.session_date or now >= opening.effective_at:
        raise ValueError('Run on the r0 trading date before its opening effective time')
    if root.exists():
        raise FileExistsError(f'Choose a new output directory; existing evidence retained: {root}')
    root.mkdir(parents=True, exist_ok=False)
    wait_until(scheduled, clock=clock, sleep=sleep)
    started = clock().astimezone(ET)
    if started >= opening.effective_at:
        raise ValueError('Opening passed before snapshot could start')
    print('Capturing Vol_test: all quote batches precede candle requests.', flush=True)
    tick = monotonic()
    batch = fetch_quotes_batched(client, opening.uni_symbols, fields='all', batch_size=400)
    snapshot_seconds = monotonic() - tick
    finished = clock().astimezone(ET)
    values, rows = {}, []
    for result in batch.results:
        payload = result.quote or {}
        fields = payload.get('quote', {})
        raw_volume = fields.get('totalVolume') if isinstance(fields, Mapping) else None
        value = volume_value(raw_volume) if result.usable else None
        values[result.symbol] = value
        rows.append({
            'symbol': result.symbol, 'status': result.status.value,
            'current_volume': value, 'batch': result.batch_number,
            'volume_available': value is not None,
            'request_started_at_utc': utc_text(result.request_started_at_utc),
            'response_received_at_utc': utc_text(result.response_received_at_utc),
            'quote': payload,
        })
    proxy_order = rank_values(values)
    proxy_top = list(proxy_order[:limit])
    frozen = {
        'contract': 'current-volume-proxy-v1', 'session_date': str(opening.session_date),
        'opening_content_sha256': opening.content_sha256,
        'scheduled_at_et': scheduled.isoformat(), 'started_at_et': started.isoformat(),
        'completed_at_et': finished.isoformat(),
        'start_lateness_seconds': max(0, (started - scheduled).total_seconds()),
        'request_count': batch.request_count, 'batch_size': 400,
        'elapsed_seconds': snapshot_seconds, 'selection_policy': 'CURRENT_VOLUME_DESC_SYMBOL_ASC',
        'symbols_requested': len(opening.uni_symbols), 'symbols_available': len(proxy_order),
        'missing_symbols': sorted(s for s, v in values.items() if v is None),
        'unexpected_symbols': list(batch.unexpected_symbols),
        'Vol_test': proxy_top, 'rows': rows,
        'reference_membership_modified': False,
    }
    # This write completes BEFORE any price_history request.
    write_json(root / 'vol_test.json', frozen)
    with (root / 'vol_test.csv').open('x', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream); writer.writerow(['rank', 'symbol', 'current_volume'])
        writer.writerows((n, s, values[s]) for n, s in enumerate(proxy_order, 1))
    print(f'Vol_test saved: {batch.request_count} quote calls; '
          f'{len(proxy_order)}/{len(opening.uni_symbols)} valid volumes.', flush=True)
    print('Acquiring accepted candle OV with the same authenticated client.', flush=True)
    tick = monotonic()
    reference = acquire_api_overnight_volume(
        client, opening.uni_symbols, trade_date=opening.session_date,
        request_interval_seconds=request_interval, max_attempts=max_attempts,
        now_factory=clock, sleep=sleep)
    reference_seconds = monotonic() - tick
    artifacts = write_api_overnight_volume_artifacts(
        root / 'api_ov', opening_proposal_path=opening_path,
        opening_content_sha256=opening.content_sha256,
        opening_uni_count=len(opening.uni_symbols), opening_effective_at=opening.effective_at,
        complete_opening_uni=True, batch=reference)
    reference_values = {o.symbol: o.ov_decision if o.usable else None
                        for o in reference.observations}
    reference_order = rank_values(reference_values)
    focus = None
    reference_error = None
    if not reference.failed_count and clock() < opening.effective_at:
        try:
            focus = focus_builder(artifacts.manifest, opening_path, root / 'focus', limit, clock())
        except Exception as error:
            # Preserve the successful acquisitions even if offline r1 generation fails.
            reference_error = type(error).__name__
    else:
        reference_error = ('symbol_failures' if reference.failed_count else 'opening_deadline_passed')
    reference_top = list(focus['symbols']) if focus else []
    if focus and reference_top != list(reference_order[:limit]):
        raise ValueError('Reference builder selection differs from saved accepted OV ordering')
    proxy_ranks = {s: i for i, s in enumerate(proxy_order, 1)}
    reference_ranks = {s: i for i, s in enumerate(reference_order, 1)}
    with (root / 'comparison.csv').open('x', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['symbol', 'current_volume', 'OV_DECISION', 'vol_test_rank',
                         'reference_rank', 'in_vol_test', 'in_focus_r1'])
        for symbol in sorted(opening.uni_symbols):
            writer.writerow([symbol, values.get(symbol), reference_values.get(symbol),
                             proxy_ranks.get(symbol), reference_ranks.get(symbol),
                             symbol in proxy_top, symbol in reference_top if focus else ''])
    common = sorted(set(proxy_top) & set(reference_top)) if focus else None
    report = {
        'contract': 'voltest-vs-candle-focus-v1', 'session_date': str(opening.session_date),
        'result': ('REFERENCE_UNAVAILABLE' if not focus else
                   'COMPARISON_COMPLETE' if not frozen['missing_symbols'] else
                   'COMPARISON_WITH_MISSING_PROXY_DATA'),
        'requested_top_n': limit, 'opening_uni_count': len(opening.uni_symbols),
        'Vol_test': proxy_top, 'Focus_r1': reference_top if focus else None,
        'common_symbols': common, 'common_count': len(common) if common is not None else None,
        'overlap_fraction_of_reference': len(common) / len(reference_top) if reference_top else None,
        'only_vol_test': sorted(set(proxy_top) - set(reference_top)) if focus else None,
        'only_focus_r1': sorted(set(reference_top) - set(proxy_top)) if focus else None,
        'proxy_missing_symbols': frozen['missing_symbols'],
        'quote_requests': batch.request_count, 'history_requests': reference.request_count,
        'quote_elapsed_seconds': snapshot_seconds, 'history_elapsed_seconds': reference_seconds,
        'proxy_start_lateness_seconds': frozen['start_lateness_seconds'],
        'reference_error': reference_error, 'reference_artifacts': focus,
        'api_ov_manifest': str(artifacts.manifest),
        'membership_published': False, 'proxy_replaced_reference': False,
        'scope': 'Selection comparison; no assumption that totalVolume equals candle OV',
    }
    write_json(root / 'comparison.json', report)
    print(json.dumps(report, indent=2), flush=True)
    return report
