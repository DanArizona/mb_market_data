"""Read-only cadence diagnostics; preserve channel and actual receipt clocks."""
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta
import math
import os
from pathlib import Path
import sqlite3
import sys
import time


def stamp(value):
    at = datetime.fromisoformat(value)
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError('Cadence evidence requires an aware timestamp')
    return at


def statistics(values):
    values = sorted(values)
    if not values:
        return dict(count=0, mean_seconds=None, p95_seconds=None, max_seconds=None)
    return dict(count=len(values), mean_seconds=round(sum(values)/len(values), 6),
                p95_seconds=round(values[math.ceil(.95*len(values))-1], 6), max_seconds=round(values[-1], 6))


def gaps(values):
    ordered = sorted(values)
    return [(b-a).total_seconds() for a,b in zip(ordered, ordered[1:])]


def group_cadence(config, rows, commits, channels, *, details=False):
    symbols = tuple(config['initial_hot'])
    start,end = stamp(config['start']),stamp(config['end'])
    seconds = {s for c in channels for s in config['grids'][c]}
    planned = set()
    at = start
    while at < end:
        if at.second in seconds:
            planned.add(at)
        at += timedelta(seconds=1)
    points = defaultdict(list)
    for row in rows:
        if row['channel'] in channels and row['symbol'] in symbols:
            points[row['symbol']].append(row)
    response_gaps,commit_gaps,scheduled_gaps = [],[],[]
    missing,unexpected,duplicates,nonquotes,no_response,no_commit = 0,0,0,0,0,0
    per_symbol = {}
    for symbol in symbols:
        selected = points[symbol]
        actual = [stamp(r['scheduled_at_utc']) for r in selected]
        missing += len(planned-set(actual))
        unexpected += len(set(actual)-planned)
        duplicates += len(actual)-len(set(actual))
        quotes = [r for r in selected if r['status']=='quote']
        nonquotes += len(selected)-len(quotes)
        responses = [stamp(r['response_received_at_utc']) for r in quotes if r['response_received_at_utc']]
        receipts = [commits[(r['channel'],stamp(r['scheduled_at_utc']))] for r in quotes
                    if (r['channel'],stamp(r['scheduled_at_utc'])) in commits]
        no_response += len(quotes)-len(responses)
        no_commit += len(quotes)-len(receipts)
        rg,cg = gaps(responses),gaps(receipts)
        response_gaps.extend(rg);commit_gaps.extend(cg);scheduled_gaps.extend(gaps(actual))
        if details:
            per_symbol[symbol] = dict(outcomes=len(selected),quotes=len(quotes),
                status_counts=dict(Counter(r['status'] for r in selected)),
                missing_slots=len(planned-set(actual)),response_gaps=statistics(rg),
                commit_observer_gaps=statistics(cg),
                first_response_at_utc=min(responses).isoformat() if responses else None,
                last_response_at_utc=max(responses).isoformat() if responses else None)
    threshold = config['response_gap_review_seconds']
    result = dict(symbol_count=len(symbols),expected_points_per_symbol=len(planned),
        expected_outcomes=len(planned)*len(symbols),recorded_outcomes=sum(map(len,points.values())),
        missing_outcomes=missing,unexpected_outcomes=unexpected,duplicate_outcomes=duplicates,
        nonquote_outcomes=nonquotes,quote_outcomes_without_response_time=no_response,
        quote_outcomes_without_commit_observer=no_commit,scheduled_gaps=statistics(scheduled_gaps),
        response_gaps=statistics(response_gaps),commit_observer_gaps=statistics(commit_gaps),
        response_gaps_over_review_threshold=sum(v>threshold for v in response_gaps),
        commit_gaps_over_review_threshold=sum(v>threshold for v in commit_gaps))
    if details:
        result['per_symbol']=per_symbol
    return result


def analyse_rows(config, rows, commits):
    groups = {name:group_cadence(config,rows,commits,channels,details=name=='all_channels')
              for name,channels in [('all_channels',('uni','focus','hot')),
                  ('uni_focus',('uni','focus')),('focus_only',('focus',)),('hot_only',('hot',))]}
    all_channels=groups['all_channels']
    complete=all(not g[k] for g in groups.values()
                 for k in ('missing_outcomes','unexpected_outcomes','duplicate_outcomes'))
    warning=any(all_channels[k] for k in ('nonquote_outcomes','quote_outcomes_without_response_time',
        'quote_outcomes_without_commit_observer','response_gaps_over_review_threshold','commit_gaps_over_review_threshold'))
    return dict(result='FAILED' if not complete else 'WARNING' if warning else 'PASS',
        outcome_coverage_complete=complete,review_threshold_seconds=config['response_gap_review_seconds'],
        cohort='40 opening-Focus symbols held in experimental Hot throughout the window',
        clock_note='Actual response receipts and post-write observer receipts, sorted per symbol; not fresh-market-data guarantees',
        **groups)


def analyse_cadence(folder, config):
    from interleaved_load import lines
    folder=Path(folder)
    commits={}
    for channel in ('uni','focus','hot'):
        for row in lines(folder/(channel+'_status.jsonl')):
            if row.get('status')=='acquired' and row.get('commit_observed_at_utc'):
                commits[(channel,stamp(row['slot_id'].split(':',1)[1]))]=stamp(row['commit_observed_at_utc'])
    with closing(sqlite3.connect((folder/'quotes.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)) as connection:
        connection.row_factory=sqlite3.Row
        rows=[dict(r) for r in connection.execute('''
            SELECT o.symbol,o.status,o.request_started_at_utc,o.response_received_at_utc,
                   a.channel,a.scheduled_at_utc,a.completed_at_utc
            FROM quote_observation o JOIN quote_acquisition a USING(acquisition_id)
            WHERE o.symbol IN ('''+','.join('?' for _ in config['initial_hot'])+')',config['initial_hot'])]
    return analyse_rows(config,rows,commits)


def analyse_overlap(calls):
    intervals=[]
    for channel,rows in calls.items():
        for row in rows:
            if row.get('dispatched') and row.get('returned_at_utc'):
                a,b=stamp(row['started_at_utc']),stamp(row['returned_at_utc'])
                if b<a:
                    raise ValueError('Quote-client return precedes dispatch receipt')
                intervals.append((a,b,channel,row['slot_id']))
    active=[]; pairs=Counter(); examples=[]; maximum=0
    for a,b,channel,slot in sorted(intervals):
        active=[r for r in active if r[1]>a]
        for x,y,other,other_slot in active:
            if other!=channel:
                key='/'.join(sorted((channel,other)))
                pairs[key]+=1
                if len(examples)<10:
                    examples.append(dict(channels=key,first_slot=other_slot,second_slot=slot,
                        overlap_seconds=round((min(b,y)-a).total_seconds(),6)))
        active.append((a,b,channel,slot));maximum=max(maximum,len(active))
    return dict(dispatched_intervals=len(intervals),cross_channel_overlap_pairs=sum(pairs.values()),
        pairs_by_channels=dict(pairs),maximum_concurrent_quote_client_calls=maximum,examples=examples,
        scope='Intervals around SDK quote calls; excludes independent production and is not a wire-level HTTP trace')


def process_resources():
    result=dict(process_cpu_seconds=round(time.process_time(),6),working_set_bytes=None,peak_working_set_bytes=None)
    try:
        if os.name=='nt':
            import ctypes
            from ctypes import wintypes
            class Counters(ctypes.Structure):
                _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[
                    (name,ctypes.c_size_t) for name in ('PeakWorkingSetSize','WorkingSetSize',
                        'QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage',
                        'QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.GetCurrentProcess.restype=wintypes.HANDLE
            psapi=ctypes.WinDLL('psapi',use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
            counters=Counters();counters.cb=ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(counters),counters.cb):
                raise OSError('Process memory counters unavailable')
            result.update(working_set_bytes=counters.WorkingSetSize,peak_working_set_bytes=counters.PeakWorkingSetSize)
        else:
            import resource
            peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            result['peak_working_set_bytes']=int(peak*(1 if sys.platform=='darwin' else 1024))
            path=Path('/proc/self/status')
            if path.is_file():
                for line in path.read_text().splitlines():
                    if line.startswith('VmRSS:'):
                        result['working_set_bytes']=int(line.split()[1])*1024
        result['memory_status']='AVAILABLE'
    except (OSError,ValueError,ImportError) as exc:
        result.update(memory_status='UNAVAILABLE',memory_error_type=type(exc).__name__)
    return result


def resource_summary(statuses):
    result={}
    for mode,rows in statuses.items():
        samples=[r['resources'] for r in rows if 'resources' in r]
        peaks=[r['peak_working_set_bytes'] for r in samples if r.get('peak_working_set_bytes') is not None]
        result[mode]=dict(samples=len(samples),peak_working_set_bytes=max(peaks) if peaks else None,
            process_cpu_seconds=samples[-1]['process_cpu_seconds'] if samples else None,
            startup_reconstruction_seconds=next((r['startup_reconstruction_seconds'] for r in rows
                if 'startup_reconstruction_seconds' in r),None),
            memory_available=bool(samples) and all(r.get('memory_status')=='AVAILABLE' for r in samples))
    return result
