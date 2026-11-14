"""Optional normal-Uni-completion handoff using its already authenticated client.

Reuses the existing directory, snapshot, Universe filters and r0 writer.
No prompts, client creation/close, automatic publication, or poller control.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from dataclasses import replace
from pathlib import Path
import time as time_module
from zoneinfo import ZoneInfo

from .daily_universe import UniverseFilterConfig, decide_daily_universe, write_daily_universe_artifacts
from .daily_universe_snapshot import (
    build_market_data_snapshot, validate_acquisition_time, validate_batch_acquisition_time,
    write_acquisition_json, write_snapshot_csv, write_snapshot_manifest,
)
from .daily_universe_workflow import WorkflowStage, workflow_paths, write_workflow_manifest
from .nasdaq_symbol_directory import (
    NASDAQ_LISTED_URL, OTHER_LISTED_URL, build_symbol_directory_snapshot,
    fetch_directory, write_symbol_directory_artifacts,
)
from .opening_hierarchy import build_opening_hierarchy, write_opening_proposal
from .schwab_quotes import fetch_quotes_batched
from .volume_proxy_experiment import wait_until, write_json

ET = ZoneInfo('America/New_York')


def build_with_client(client, *, session_date: date, target_date: date,
                      output_dir: Path, clock=lambda: datetime.now(ET),
                      directory_fetch=fetch_directory, exchange_close=None,
                      require_complete_transport=False, opening_focus_policy=None) -> dict:
    """Regular-session baseline: preserve the existing same-day >=16:00 gate."""
    if target_date <= session_date or target_date.weekday() >= 5:
        raise ValueError('Supply the actual next trading date, later than the source date')
    validated_at = clock()
    validate_acquisition_time(session_date, validated_at, exchange_close=exchange_close)
    paths = workflow_paths(Path(output_dir).resolve())
    if paths.root.exists():
        raise FileExistsError(f'Existing build is retained, not overwritten: {paths.root}')
    paths.root.mkdir(parents=True, exist_ok=False)
    print('POST-CLOSE: downloading broad directory; reusing Uni Schwab client.', flush=True)
    directory = build_symbol_directory_snapshot(
        directory_fetch(NASDAQ_LISTED_URL, timeout=30),
        directory_fetch(OTHER_LISTED_URL, timeout=30))
    directory_paths = write_symbol_directory_artifacts(
        output_dir=paths.symbol_directory, snapshot=directory, retrieved_at_et=clock())
    candidates = directory.candidate_rows
    if not candidates:
        raise ValueError('Directory returned no eligible candidates')
    validate_acquisition_time(session_date, clock(), exchange_close=exchange_close)
    batch = fetch_quotes_batched(client, [r['symbol'] for r in candidates],
                                 fields='all', batch_size=400)
    paths.market_snapshot.mkdir()
    acquisition = paths.market_snapshot / 'quote_acquisition.json'
    # Preserve the returned snapshot even if a later date/validity check fails.
    write_acquisition_json(acquisition, batch, session_date=session_date)
    validate_batch_acquisition_time(session_date, batch, exchange_close=exchange_close)
    if require_complete_transport and any(r.status.value not in ('quote','invalid') for r in batch.results):
        raise ValueError('Incomplete broad snapshot transport; retained raw data, no reduced roster published')
    rows = build_market_data_snapshot(candidates, batch, session_date=session_date)
    snapshot_csv = paths.market_snapshot / 'market_data_snapshot.csv'
    write_snapshot_csv(snapshot_csv, rows)
    write_snapshot_manifest(
        paths.market_snapshot / 'manifest.json', session_date=session_date,
        candidate_path=directory_paths['candidates'], snapshot_path=snapshot_csv,
        acquisition_path=acquisition, acquisition=batch, rows=rows,
        validated_at=validated_at, created_at_utc=clock(), exchange_close=exchange_close)
    config = UniverseFilterConfig()
    decisions = decide_daily_universe(directory.normalized_rows, rows, config)
    write_daily_universe_artifacts(
        output_dir=paths.universe, decisions=decisions,
        symbol_directory_path=directory_paths['normalized'], market_data_path=snapshot_csv,
        session_date=session_date, target_date=target_date, config=config,
        generated_at_utc=clock())
    opening = build_opening_hierarchy(paths.universe)
    if not opening.uni_symbols:
        raise ValueError('No symbols qualified; no usable r0 will be claimed')
    if opening_focus_policy is not None:
        opening=replace(opening,metadata={**dict(opening.metadata),'opening_focus_policy':opening_focus_policy})
    write_opening_proposal(paths.opening_hierarchy, opening)
    stages = tuple(WorkflowStage(name, ('in-process', function)) for name, function in (
        ('Nasdaq symbol directory', 'build_symbol_directory_snapshot'),
        ('Schwab post-close snapshot using existing client', 'fetch_quotes_batched'),
        ('Deterministic universe build', 'decide_daily_universe'),
        ('Opening schema-v2 r0 proposal', 'build_opening_hierarchy')))
    write_workflow_manifest(paths=paths, stages=stages, session_date=session_date,
                            target_date=target_date, created_at_utc=clock())
    result = {'status': 'R0_BUILT_NOT_PUBLISHED', 'source_session': str(session_date),
              'target_session': str(target_date), 'candidate_symbols': len(candidates),
              'quote_requests': batch.request_count, 'uni_symbols': len(opening.uni_symbols),
              'opening_proposal': str(paths.opening_hierarchy),
              'workflow_manifest': str(paths.manifest), 'used_existing_client': True,
              'additional_password_prompts': 0, 'membership_published': False}
    write_json(paths.root / 'postclose_result.json', result)
    print(f"POST-CLOSE r0 built: {len(opening.uni_symbols)} symbols; {paths.opening_hierarchy}", flush=True)
    return result


def handoff(client, *, scheduled_completion: bool, channel: str,
            window_end: datetime, target_date: date, output_dir: Path,
            delay_seconds: float = 60.0, clock=lambda: datetime.now(ET),
            sleep=time_module.sleep, builder=build_with_client) -> dict:
    """Only a scheduled Uni finish can invoke the build; never an early stop."""
    if not scheduled_completion or channel != 'uni':
        return {'status': 'NOT_RUN', 'reason': 'not_normal_uni_completion'}
    if not 0 <= delay_seconds <= 3600:
        raise ValueError('Post-close delay must be between 0 and 3600 seconds')
    end = window_end.astimezone(ET)
    if end.time() != time(16):
        raise ValueError('This candidate retains the installed regular-day 16:00 close gate')
    due = end + timedelta(seconds=delay_seconds)
    print(f'POST-CLOSE r0 will use the existing client at/after {due.isoformat()}.', flush=True)
    wait_until(due, clock=clock, sleep=sleep)
    validate_acquisition_time(end.date(), clock())
    return builder(client, session_date=end.date(), target_date=target_date,
                   output_dir=output_dir, clock=clock)
