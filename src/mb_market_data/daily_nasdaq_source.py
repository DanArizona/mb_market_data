"""Paced Nasdaq source -> shared membership inbox; never a membership writer.

Reuses the existing Nasdaq RSS downloader/parser. CURRENT and same-day HISTORY
alternate, one request per minute total. Raw and normalized captures are retained.
The first successful source start establishes a persisted admission cutoff: old
startup episodes are visible evidence, not new automatic promotions.
"""
from __future__ import annotations
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import xml.etree.ElementTree as XML
from uuid import uuid4

from .acquisition_health import atomic_json, operation_lock
from .nasdaq_halts import fetch_trade_halts, normalize_nasdaq_xml, VOLATILITY_REASON_CODES
from .daily_membership import timestamp, normalized, read, fingerprint

UTC=timezone.utc

def halt_time(record):
    text=record.halt_date.strip()
    try:day=datetime.strptime(text,'%m/%d/%Y').date()
    except ValueError:day=datetime.fromisoformat(text).date()
    from .daily_flow import ET
    return datetime.combine(day,datetime.strptime(record.halt_time.strip(),'%H:%M:%S.%f' if '.' in record.halt_time else '%H:%M:%S').time(),tzinfo=ET).astimezone(UTC)


def validate_feed(feed):
    root=XML.fromstring(normalize_nasdaq_xml(feed.raw_xml))
    if root.tag!='rss' or root.find('channel') is None:
        raise ValueError('Response is not a Nasdaq RSS channel; an HTML/error response is not an empty feed')


def accept_capture(plan,feed,*,owner_pid,eligible_since,root):
    """Save feed evidence BEFORE submitting events. No synthetic live inputs."""
    validate_feed(feed)
    observed=feed.retrieved_at_et.astimezone(UTC)
    stamp=observed.strftime('%H%M%S%f')+'-'+uuid4().hex[:4]
    captures=root/'captures';captures.mkdir(exist_ok=True,parents=True)
    raw=captures/(stamp+'.xml');raw.write_bytes(feed.raw_xml)
    payload={'observed_at':observed.isoformat(),'mode':feed.retrieval_mode,'url':feed.url,
        'raw_sha256':hashlib.sha256(feed.raw_xml).hexdigest(),'records':[asdict(r) for r in feed.records]}
    atomic_json(captures/(stamp+'.json'),payload)
    inbox=root.parent/'membership'/'inbox';inbox.mkdir(exist_ok=True,parents=True)
    receipts=root.parent/'membership'/'receipts'
    events_path=root/'events.json'
    events=read(events_path) if events_path.is_file() else {}
    queued=0;bad=0;ignored=0
    for record in feed.records:
        try:
            halted=halt_time(record);symbol=normalized(record.symbol)
        except (ValueError,TypeError):bad+=1;continue
        key=symbol+'@'+halted.isoformat()
        if timestamp(plan['opens_at']).date()==halted.date() and halted<=observed:
            previous=events.get(key,{})
            row=asdict(record)
            for field in ('resumption_date','resumption_quote_time','resumption_trade_time'):
                if not row.get(field) and previous.get('record',{}).get(field):
                    row[field]=previous['record'][field]
            events[key]={'record':row,'first_observed_at':previous.get('first_observed_at',observed.isoformat()),
                'last_observed_at':observed.isoformat(),'capture_file':str(captures/(stamp+'.json'))}
        if record.reason_code.strip().upper() not in VOLATILITY_REASON_CODES:continue
        if halted<max(timestamp(plan['opens_at']),eligible_since) or halted>observed or halted>=timestamp(plan['closes_at']):
            ignored+=1;continue
        # Identity excludes mutable reason/resumption fields. The same halt can
        # update its release time without creating another promotion command.
        eid=hashlib.sha256((symbol+'|'+halted.isoformat()).encode()).hexdigest()
        cid='nasdaq-'+eid
        if (receipts/(cid+'.json')).is_file() or (inbox/(cid+'.json')).is_file():continue
        command={'id':cid,'source':'nasdaq','action':'promote','target':'hot','symbol':symbol,
                 'halted_at':halted.isoformat(),'submitted_at':observed.isoformat(),
                 'owner_pid':owner_pid,'opening_content_sha256':plan['opening_content_sha256'],
                 'capture_file':str(captures/(stamp+'.json'))}
        atomic_json(inbox/(cid+'.json'),command);queued+=1
    payload.update(queued=queued,invalid_rows=bad,startup_or_other_session_ignored=ignored)
    atomic_json(events_path,events)
    atomic_json(root/'latest.json',payload)
    return payload


def run(plan,*,owner_pid,stop,clock=lambda:datetime.now(UTC),sleep=time.sleep,fetcher=fetch_trade_halts):
    root=Path(plan['state_root'])/'nasdaq';root.mkdir(exist_ok=True,parents=True)
    interval=max(60,float(plan.get('nasdaq_interval_seconds',60)))
    with operation_lock('daily-nasdaq:'+str(Path(plan['journal']).resolve()).casefold()):
        binding_path=root/'binding.json'
        if binding_path.is_file():
            binding=read(binding_path)
            if binding['opening_content_sha256']!=plan['opening_content_sha256']:raise ValueError('Nasdaq source bound to another session')
            since=timestamp(binding['eligible_since'])
        else:
            since=clock().astimezone(UTC)
            atomic_json(binding_path,{'eligible_since':since.isoformat(),'opening_content_sha256':plan['opening_content_sha256']})
        index=0;next_poll=0;last_success=None;errors=0
        while not stop() and clock()<timestamp(plan['closes_at']):
            if time.monotonic()<next_poll:
                sleep(min(.25,max(0,next_poll-time.monotonic())));continue
            # Pace start-to-start with real elapsed time, including failures.
            next_poll=time.monotonic()+interval
            mode='CURRENT' if index%2==0 else 'HISTORICAL';index+=1
            try:
                feed=fetcher(None if mode=='CURRENT' else datetime.fromisoformat(plan['session_date']).date(),timeout=8)
                if stop():break
                value=accept_capture(plan,feed,owner_pid=owner_pid,eligible_since=since,root=root)
                last_success=clock().isoformat();state='RUNNING';error=None
            except Exception as exc:
                errors+=1;state='STALE';error=type(exc).__name__
                # Failure affects automatic discovery, not active membership or
                # manual controls. Never clear Hot because a feed is unavailable.
                value={}
            atomic_json(root/'status.json',{'state':state,'at':clock().isoformat(),'pid':os.getpid(),
                'owner_pid':owner_pid,'last_success_at':last_success,'errors':errors,'error_type':error,
                'mode':mode,'request_interval_seconds':interval,'queued':value.get('queued',0)})
        atomic_json(root/'status.json',{'state':'STOPPED','at':clock().isoformat(),'pid':os.getpid(),
                    'owner_pid':owner_pid,'last_success_at':last_success,'errors':errors})
    return 0
