"""Explicit opening Hot seeds: normal atomic membership, never ranking inputs.

This is operator test membership, not Nasdaq admission. The saved seed revision
is published once before the open. Resume does not reapply it over later manual
changes. The native collector resolves current membership at every slot.
"""
from datetime import date, datetime
from pathlib import Path
from .sampling_membership import SamplingHierarchyRevision
from .quote_observation_store import QuoteObservationStore
from .daily_flow import read_revision, revision_payload
from .acquisition_health import atomic_json
from .schwab_quotes import fetch_quotes_batched, normalize_symbols

DEFAULT_HOT = ('MSFT', 'INTC', 'NVDA')


def configure(plan, symbols=DEFAULT_HOT):
    symbols=normalize_symbols(symbols)
    if not 1 <= len(symbols) <= 40:
        raise ValueError('Opening Hot requires 1 through 40 distinct symbols')
    return dict(plan, hot_activated=True, hot_seed_symbols=list(symbols),
        channels=['uni','focus','hot'], independent_monitor=True,
        hot_seed_source='operator-test-selection-v1',
        operator_authorization='foreground-attended-v1')


def validate_seeds(client, plan, output, *, clock):
    """One bounded batch validates exact quote identity; no inclusion filter bypass
    is disguised as ranked Universe selection. Raw quote evidence is retained.
    """
    symbols=plan.get('hot_seed_symbols',[])
    if not symbols:return None
    deadline=datetime.fromisoformat(plan['selection_deadline'])
    if clock()>=deadline:
        report={'status':'UNAVAILABLE','reason':'seed_validation_deadline','requested':symbols}
    else:
        batch=fetch_quotes_batched(client,symbols,fields='all',batch_size=400)
        rows=[]
        for item in batch.results:
            q=item.quote or {}
            valid=(item.usable and q.get('symbol')==item.symbol and q.get('assetMainType')=='EQUITY'
                   and item.response_received_at_utc is not None and item.response_received_at_utc<deadline)
            rows.append({'symbol':item.symbol,'valid':bool(valid),'status':item.status.value,'quote':q,
                'requested_at':item.request_started_at_utc.isoformat(),
                'received_at':item.response_received_at_utc.isoformat() if item.response_received_at_utc else None})
        report={'status':'READY' if len(rows)==len(symbols) and all(r['valid'] for r in rows) else 'UNAVAILABLE',
                'source':'operator-test-selection-v1','requested':symbols,'rows':rows,
                'quote_requests':batch.request_count,'completed_at':clock().isoformat()}
    atomic_json(Path(output)/'hot-seed-validation.json',report)
    return report


def publish_seeds(plan, *, now):
    root=Path(plan['state_root'])/'selection'
    store=QuoteObservationStore(plan['journal'],session_date=date.fromisoformat(plan['session_date']),schema_version=2)
    receipt=root/'hot-seed-publication.json'
    if receipt.exists():
        import json
        saved=json.loads(receipt.read_text(encoding='utf-8'))
        # Reconfirm the original revision only. This never removes a later demotion.
        original=read_revision(root/'hot-seed-r2.json')
        if saved['content_sha256']!=original.content_sha256:raise ValueError('Seed evidence differs')
        store.record_membership_revision(original)
        return saved
    if now>=datetime.fromisoformat(plan['opens_at']):
        raise ValueError('Opening seed publication cannot be backdated after the open')
    import json
    proof=json.loads((root/'hot-seed-validation.json').read_text(encoding='utf-8'))
    if proof.get('status')!='READY' or proof.get('requested')!=plan['hot_seed_symbols']:
        raise ValueError('Opening Hot symbols lack complete quote-identity evidence')
    r1=read_revision(root/'r1.json');seeds=set(plan['hot_seed_symbols'])
    r2=SamplingHierarchyRevision(session_date=r1.session_date,revision=2,effective_at=r1.effective_at,
        uni_symbols=tuple(sorted(set(r1.uni_symbols)|seeds)),
        focus_symbols=tuple(sorted(set(r1.focus_symbols)|seeds)),hot_symbols=tuple(sorted(seeds)),
        source='operator-test-selection-v1',reason='Opening Hot test roster; ranked Focus forty unchanged',
        metadata={'ranked_focus_symbols':list(r1.metadata['ranked_focus_symbols']),
                  'hot_seed_symbols':list(plan['hot_seed_symbols']),
                  'base_r1_content_sha256':r1.content_sha256,'hot_seed_validation':str(root/'hot-seed-validation.json')})
    proposal=root/'hot-seed-r2.json'
    if proposal.exists():
        if read_revision(proposal).content_sha256!=r2.content_sha256:raise ValueError('Existing seed proposal differs')
    else:atomic_json(proposal,revision_payload(r2))
    store.record_membership_revision(r2)
    result={'status':'PUBLISHED','revision':2,'content_sha256':r2.content_sha256,
            'hot_symbols':list(r2.hot_symbols),'focus_count':len(r2.focus_symbols),'uni_count':len(r2.uni_symbols),
            'effective_at':r2.effective_at.isoformat(),'confirmed_at':now.isoformat()}
    atomic_json(receipt,result)
    return result


def seed_is_published(plan):
    """Used on resume; never select new symbols or republish at a later instant."""
    root=Path(plan['state_root'])/'selection'
    if not (root/'hot-seed-publication.json').is_file():return False
    publish_seeds(plan,now=datetime.fromisoformat(plan['opens_at']))
    return True
