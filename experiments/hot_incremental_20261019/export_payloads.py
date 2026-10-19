"""Read-only export of successful quote payload samples from a known isolated trial."""
from __future__ import annotations
import hashlib,json,sqlite3,uuid
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED

KNOWN_TRIAL=Path(r'C:\Users\danla\Documents\github\mb_market_data\output\hot_scale_20261019_002119_df5781\runs\hot_scale_schwab\20261019T015628Z-8ee830fd')

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()

def export(source,output):
    source=Path(source).resolve();output=Path(output).resolve()
    # Fixed phase layout, read-only handles, no credential/config/token file access.
    if any(p.lower().startswith('quote_observation_journal') for p in source.parts):raise ValueError('Production paths excluded')
    result={'contract':'successful-quote-payload-samples-v1','source_trial':str(source),'phases':[],'limitations':['Selected successful decoded quote payloads, not transport headers or error bodies','Original source timestamps preserved; no arrival-time capacity claim']}
    for name in ('phase_01_2s','phase_02_4s'):
        path=source/name/'quotes.hot.poller.sqlite3'
        if not path.is_file():raise ValueError('Expected isolated trial sidecar missing: '+str(path))
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as connection:
            identity=json.loads(connection.execute('SELECT payload FROM acquisition_identity WHERE id=1').fetchone()[0])
            if identity.get('contract')!='isolated_hot_acquisition_v1' or identity.get('settings',{}).get('watchlist_kind')!='hot':raise ValueError('Unexpected source trial identity')
            prior=digest(identity);assembled=[]
            for expected,(sequence,raw,previous,sha) in enumerate(connection.execute('SELECT * FROM acquisition_event ORDER BY sequence'),1):
                value=json.loads(raw)
                if sequence!=expected or previous!=prior or sha!=digest({'sequence':sequence,'payload':value,'previous_sha256':previous}):raise ValueError('Source acquisition chain differs')
                prior=sha
                if value['kind']=='assembled':assembled.append((sequence,sha,value))
        if not assembled:raise ValueError('No saved assembled response in '+name)
        samples=[]
        for index in sorted({0,len(assembled)//2,len(assembled)-1}):
            sequence,sha,value=assembled[index]
            quotes=[{k:row.get(k) for k in ('symbol','status','quote','request_started_at_utc','response_received_at_utc')} for row in value['result']['results'] if row['status']=='quote']
            if not quotes or len(quotes)>80:raise ValueError('Unexpected successful sample size')
            samples.append({'source_sequence':sequence,'source_event_sha256':sha,'source_recorded_at_utc':value['recorded_at_utc'],'source_completed_at_utc':value['completed_at_utc'],'quotes':quotes})
        result['phases'].append({'name':name,'source_identity_sha256':digest(identity),'source_chain_head_sha256':prior,'assembled_count':len(assembled),'samples':samples})
    data=(json.dumps(result,indent=2,sort_keys=True,allow_nan=False)+'\n').encode()
    if len(data)>10*1024*1024:raise ValueError('Export unexpectedly large')
    output.mkdir(parents=True,exist_ok=True)
    target=output/('hot_quote_payload_samples_'+datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]+'.zip')
    with ZipFile(target,'x',ZIP_DEFLATED) as archive:archive.writestr('quote_payload_samples.json',data)
    return target

if __name__=='__main__':
    try:
        target=export(KNOWN_TRIAL,Path(__file__).resolve().parents[2]/'runs/payload_exports')
        print('Read-only successful quote sample export: PASS',flush=True)
        print('Attach this ZIP: '+str(target),flush=True)
        print('No password or API request. Command returns to prompt.',flush=True)
    except Exception as exc:
        print('Quote sample export FAILED: '+str(exc),flush=True);raise SystemExit(1)
