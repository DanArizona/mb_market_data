"""Read-only live adapter using the existing Observation Overlay figure.

No SDK/auth imports, provider requests or journal writes. No fake OHLCV cache is
constructed: quote-only mode explicitly supplies no candles. A separate local
viewer failure cannot stop collection. Historical replay/candles remain in the
existing full dashboard, unchanged apart from accepting a missing live cache.
"""
from __future__ import annotations
import argparse
from contextlib import closing
from datetime import date, datetime, time, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs,urlparse
from types import MappingProxyType
from zoneinfo import ZoneInfo

from .sampling_membership import SAMPLING_HIERARCHY_CONTRACT
from .quote_observation_store import OBSERVATION_COLUMNS
from .observation_overlay import ObservationOverlayData, OverlayMembershipTransition, OverlayQuotePoint, MembershipBand
from .observation_overlay_view import build_observation_overlay_figure
from .daily_membership import normalized,timestamp,read
from .polling_cadence import cadence_seconds
from .overlay_halts import OverlayHaltInterval

UTC=timezone.utc;ET=ZoneInfo('America/New_York')


def overlay_snapshot(root, symbol, *, at=None, limit=12000):
    root=Path(root).resolve();plan=read(root/'plan.json');symbol=normalized(symbol)
    if not 1<=limit<=20000:raise ValueError('Bounded point limit required')
    now=at or datetime.now(UTC);day=date.fromisoformat(plan['session_date'])
    # A finished session remains viewable after midnight, without claiming live.
    cutoff=min(now.astimezone(UTC),datetime.combine(day,time(23,59,59,999999),ET).astimezone(UTC))
    text=cutoff.isoformat(timespec='microseconds').replace('+00:00','Z')
    path=Path(plan['journal']).resolve();transitions=[];band=MembershipBand.OUTSIDE_UNI
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.3)) as c:
        c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
        ident=c.execute('SELECT * FROM store_identity WHERE singleton=1').fetchone()
        if ident is None or ident['schema_version']!=2 or ident['session_date']!=plan['session_date'] or ident['membership_contract']!=SAMPLING_HIERARCHY_CONTRACT:
            raise ValueError('Session journal identity differs')
        revisions=c.execute('SELECT revision,effective_at_utc,source,reason FROM sampling_membership_revision WHERE effective_at_utc<=? ORDER BY effective_at_utc,revision',(text,)).fetchall()
        rows=c.execute('SELECT revision,channel FROM sampling_channel_member WHERE symbol=?',(symbol,)).fetchall()
        tiers={}
        for row in rows:tiers.setdefault(row['revision'],set()).add(row['channel'])
        for r in revisions:
            active=tiers.get(r['revision'],set())
            value=next((MembershipBand(k) for k in ('hot','focus','uni') if k in active),MembershipBand.OUTSIDE_UNI)
            if value!=band:
                transitions.append(OverlayMembershipTransition(timestamp(r['effective_at_utc']),r['revision'],value,r['source'],r['reason']));band=value
        revision=revisions[-1]['revision'] if revisions else None
        members={k:[] for k in ('uni','focus','hot')}
        if revision is not None:
            for r in c.execute('SELECT channel,symbol FROM sampling_channel_member WHERE revision=? ORDER BY channel,symbol',(revision,)):
                members[r['channel']].append(r['symbol'])
        quotes=c.execute('''SELECT o.*, a.channel,a.channel_revision,a.scheduled_at_utc,a.completed_at_utc,a.run_id
             FROM quote_observation o JOIN quote_acquisition a ON a.acquisition_id=o.acquisition_id
             WHERE o.symbol=? AND a.completed_at_utc<=? ORDER BY a.completed_at_utc DESC,a.acquisition_id DESC LIMIT ?''',(symbol,text,limit+1)).fetchall()
        truncated=len(quotes)>limit;quotes=quotes[:limit]
        points=tuple(OverlayQuotePoint(timestamp(r['completed_at_utc']),timestamp(r['scheduled_at_utc']),r['acquisition_id'],
            r['channel'],r['channel_revision'],r['status'],r['detail'],MappingProxyType({k:r[k] for k in OBSERVATION_COLUMNS}),
            cadence_seconds(r['channel'],'interleaved-v1')) for r in reversed(quotes))
        pending=c.execute('SELECT revision,effective_at_utc FROM sampling_membership_revision WHERE effective_at_utc>? ORDER BY revision',(text,)).fetchall()
    observed_halts=[];intervals=[]
    try:
        events=read(root/'nasdaq'/'events.json')
        for event in events.values():
            record=event['record']
            if record.get('symbol')!=symbol or timestamp(event['last_observed_at'])>cutoff:continue
            # Live knowledge only; do not present a later release update as
            # information available before that capture was received.
            hdate=datetime.strptime(record['halt_date'],'%m/%d/%Y').date() if '/' in record['halt_date'] else date.fromisoformat(record['halt_date'])
            start=datetime.fromisoformat(str(hdate)+'T'+''.join(record['halt_time'].split())).replace(tzinfo=ET)
            end=None
            if record.get('resumption_trade_time') and record.get('resumption_date'):
                text_date=record['resumption_date']
                rday=datetime.strptime(text_date,'%m/%d/%Y').date() if '/' in text_date else date.fromisoformat(text_date)
                end=datetime.fromisoformat(str(rday)+'T'+''.join(record['resumption_trade_time'].split())).replace(tzinfo=ET)
            if hdate==day and start<=cutoff and (end is None or start<=end<=cutoff):
                intervals.append(OverlayHaltInterval(start,end,record['reason_code']))
                observed_halts.append(event)
    except (OSError,ValueError,KeyError,TypeError):pass
    data=ObservationOverlayData(day,symbol,cutoff,None,(),tuple(transitions),points,tuple(intervals))
    status={};status_error=None
    try:status=read(root/'status.json')
    except (OSError,ValueError) as e:status_error=type(e).__name__
    observed=status.get('at');age=(now-timestamp(observed)).total_seconds() if observed else None
    final=status.get('final_status') is True
    alerts={}
    try:alerts=read(root/'health'/'latest.json').get('daily_conditions',{})
    except (OSError,ValueError):pass
    info={'session_date':plan['session_date'],'symbol':symbol,'at':cutoff.isoformat(),
        'membership':band.value,'membership_revision':revision,'members':members,
        'points':len(points),'point_limit':limit,'history_truncated':truncated,
        'pending_revisions':[dict(r) for r in pending], 'health':status.get('health',{}),
        'phase':status.get('phase','UNAVAILABLE'),'health_age_seconds':age,'final':final,
        'health_stale':not final and (age is None or age>30), 'health_read_error':status_error,
        'services':status.get('services',{}),'alerts':alerts,'observed_halts':observed_halts,
        'mode':'QUOTE_ONLY; no candles acquired; existing Overlay figure reused',
        'read_only':True,'extra_provider_calls':0}
    return data,info


def rendered_snapshot(root,symbol):
    data,info=overlay_snapshot(root,symbol)
    figure=build_observation_overlay_figure(data,theme='dark',units='price')
    # Keep user zoom/selection on refresh; changing symbols resets it.
    figure.update_layout(uirevision=data.symbol)
    return {'figure':json.loads(figure.to_json()),'status':info}


PAGE='''<!doctype html><html><head><meta charset="utf-8"><title>MasterBot Live Observation Overlay</title>
<style>body{background:#111820;color:#ecf0f3;font:16px system-ui;margin:20px}button,select,input{font:inherit;padding:5px}#warning{color:#ffbd59;white-space:pre-wrap}pre{white-space:pre-wrap}summary{cursor:pointer}#chart{min-height:700px}</style>
<script src="/plotly.js"></script></head><body>
<h2>MasterBot · Live Observation Overlay</h2>
<p>Read-only · Saved quote observations and effective membership · No extra market-data requests</p>
<label>Symbol <select id="symbol"></select></label> <button id="pause">Pause view</button>
<span id="headline"></span><p id="warning"></p><div id="chart"></div>
<details><summary>Collection health, membership and active alerts</summary><pre id="status"></pre></details>
<p>Quote-only view. No candles have been fetched or fabricated. Closing this page does not stop collection. Ctrl+C in the viewer console stops only this server.</p>
<script>
let busy=false, paused=false, initial=true;
const symbol=document.getElementById('symbol');
const start=new URLSearchParams(location.search).get('symbol')||'MSFT';
symbol.add(new Option(start,start));symbol.value=start;
document.getElementById('pause').onclick=()=>{paused=!paused;document.getElementById('pause').textContent=paused?'Return to live':'Pause view';if(!paused)tick();};
symbol.onchange=()=>tick(true);
async function tick(force=false){if(busy||(paused&&!force))return;busy=true;
try{const r=await fetch('/snapshot?symbol='+encodeURIComponent(symbol.value),{cache:'no-store'});if(!r.ok)throw Error(await r.text());const v=await r.json();const s=v.status;
const selected=symbol.value;const choices=Array.from(new Set([...s.members.hot,...s.members.focus,...s.members.uni,selected])).sort();symbol.replaceChildren(...choices.map(x=>new Option(x,x)));symbol.value=selected;
await Plotly.react('chart',v.figure.data,v.figure.layout,{responsive:true,displaylogo:false});
document.getElementById('headline').textContent=' '+s.membership.toUpperCase()+' · '+s.points+' saved observations · '+s.phase;
document.getElementById('warning').textContent=[s.final?'FINAL SESSION REPORT — not live':s.health_stale?'HEALTH STATUS STALE — do not infer live collection':'',s.history_truncated?'Only most recent '+s.point_limit+' observations displayed; full journal retained.':'',...Object.values(s.alerts)].filter(Boolean).join('\\n');
document.getElementById('status').textContent=JSON.stringify(s,null,2);
}catch(e){document.getElementById('warning').textContent='VIEW DATA UNAVAILABLE: '+e.message+' — previous chart retained, not proof of live progress.';}finally{busy=false;}}
tick();setInterval(tick,3000);
</script></body></html>'''


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--state-root',type=Path,required=True)
    p.add_argument('--symbol',default='MSFT');p.add_argument('--port',type=int,default=8053)
    a=p.parse_args(argv)
    if not 1024<=a.port<=65535:raise ValueError('Use a local port from 1024 through 65535')
    symbol=normalized(a.symbol);root=a.state_root.resolve()
    from plotly.offline import get_plotlyjs
    javascript=get_plotlyjs().encode('utf-8')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):self.send_error(405,'Read-only viewer')
        def do_GET(self):
            parsed=urlparse(self.path)
            try:
                if parsed.path=='/':content=PAGE.encode();kind='text/html; charset=utf-8'
                elif parsed.path=='/plotly.js':content=javascript;kind='application/javascript'
                elif parsed.path=='/snapshot':
                    requested=parse_qs(parsed.query).get('symbol',[symbol])[0]
                    content=json.dumps(rendered_snapshot(root,requested),allow_nan=False).encode();kind='application/json'
                else:self.send_error(404);return
                self.send_response(200);self.send_header('Content-Type',kind);self.send_header('Cache-Control','no-store')
                self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
            except (OSError,sqlite3.Error,ValueError,KeyError,TypeError) as error:
                content=('Read-only view could not load current data: '+type(error).__name__).encode()
                self.send_response(503);self.send_header('Content-Type','text/plain');self.end_headers();self.wfile.write(content)
    server=ThreadingHTTPServer(('127.0.0.1',a.port),Handler);server.daemon_threads=True
    print('READ-ONLY LIVE OVERLAY; no credentials or provider calls.')
    print(f'Open http://127.0.0.1:{a.port}/?symbol={symbol}')
    print('Ctrl+C stops this viewer only. Collectors continue.')
    try:server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:print('Viewer stopped. Collection unchanged.')
    finally:server.server_close()
    return 0
