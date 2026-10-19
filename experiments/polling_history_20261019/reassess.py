"""Read-only derivation of a separate assessment for the frozen legacy trial."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3


def fingerprint(folder):
    # Read-only WAL connections may create an empty WAL/SHM pair. An empty
    # WAL carries no database frames; compare the DB and every nonempty WAL.
    return {name:hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in ('report.json','quotes.sqlite3','quotes.sqlite3-wal') if (folder/name).is_file() and (not name.endswith('-wal') or (folder/name).stat().st_size>0)}


def reassess(folder,output,on_status=print):
    from history_proof import read_json,write_json,require
    from mb_market_data.sampling_assessment import assess_legacy_report
    from mb_market_data.quote_journal_audit import audit_quote_journal
    from mb_market_data.hot_provider_trial import replay_digest
    folder,output=Path(folder).resolve(),Path(output).resolve()
    require(output!=folder and not output.is_relative_to(folder),'Derived assessment must be separate')
    require(not output.exists(),'Fresh derived output required')
    require(not any(p.lower().startswith('quote_observation_journal') for p in folder.parts),'Production journals excluded')
    require((folder/'report.json').stat().st_size<5_000_000,'Unexpected original report size')
    original=read_json(folder/'report.json')
    before=fingerprint(folder)
    assessment=assess_legacy_report(original)
    on_status('Read-only audit and two replays of the original experiment. Original verdict remains '+original['result']+'.')
    audit=audit_quote_journal(folder/'quotes.sqlite3',audited_at=__import__('datetime').datetime.fromisoformat(original['logical_end']))
    require(audit.passed,'Independent audit failed')
    first=replay_digest(folder/'quotes.sqlite3');second=replay_digest(folder/'quotes.sqlite3')
    require(first==second==original['journal_replay'],'Independent replays differ from the frozen report')
    counts={c.channel:dict(c.status_counts) for c in audit.channels}
    require(counts=={c:v['status_counts'] for c,v in original['channels'].items()},'Journal/report status counts differ')
    with closing(sqlite3.connect((folder/'quotes.sqlite3').as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('PRAGMA query_only=ON')
        rows=c.execute('SELECT a.channel,o.symbol,o.status,COUNT(*),MIN(a.scheduled_at_utc),MAX(a.scheduled_at_utc) FROM quote_observation o JOIN quote_acquisition a USING(acquisition_id) WHERE o.status<>? GROUP BY a.channel,o.symbol,o.status ORDER BY a.channel,o.symbol,o.status',('quote',)).fetchall()
    require(fingerprint(folder)==before,'Original report or journal changed during read-only assessment')
    value=dict(assessment,contract='derived-combined-assessment-v1',source_run=str(folder),source_hashes=before,
        independent_audit_passed=True,independent_replays_identical=True,journal_replay=first,
        unavailable_outcomes=[dict(zip(('channel','symbol','status','count','first_slot_utc','last_slot_utc'),r)) for r in rows],
        interpretation='Explicit provider-invalid results are availability warnings; no reason or tradability conclusion is inferred.')
    output.mkdir(parents=True)
    write_json(output/'assessment.json',value)
    summary=['DERIVED COMBINED ASSESSMENT: '+value['result'], 'Original result: '+original['result']+' (preserved)',
        'Collection/integrity: '+value['collection_integrity']['result']+'; transport: '+value['transport']['result']+'; availability: '+value['availability']['result'],
        'Independent audit and two matching replays: PASS',*[str(row) for row in value['unavailable_outcomes']],value['interpretation']]
    (output/'summary.txt').write_text('\n'.join(summary)+'\n',encoding='utf-8')
    on_status('\n'.join(summary))
    return value
