"""Daily Hot acquisition over the verified native response/ACK ledger.

This trusted API has no authentication or live launcher. A bounded owning
controller supplies the publication validator and client. The short isolated
poller's constructor remains sealed against production opening journals.
"""
from collections import OrderedDict
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .polling_cadence import RESERVED_HOT_SECONDS, check_before_credentials
from .quote_observation_store import PollRunProvenance
from .sampling_acquisition import IsolatedSamplingPoller, canonical, digest
from .sampling_membership import SamplingChannel

CONTRACT = 'daily-native-hot-acquisition-v2'


class DailyHotPoller(IsolatedSamplingPoller):
    """Use the accepted durable dispatch/assembled-response/commit/ACK engine."""
    def __init__(self, journal, *, settings, client, daily_binding, daily_claim,
                 publication_binding, validate_publication, clock=None,
                 activation_approved=False, max_quote_calls=9360):
        if activation_approved is not True:
            raise ValueError('Explicit trusted activation authorization required')
        if not callable(validate_publication) or not callable(getattr(client, 'quotes', None)):
            raise ValueError('Bound publication validator and quote client required')
        self.daily_binding = json.loads(canonical(daily_binding))
        if (type(max_quote_calls) is not int or not 1 <= max_quote_calls <=
                self.daily_binding['policy']['limits']['maximum_hot_quote_calls']):
            raise ValueError('Explicit bounded daily native quote-call ceiling required')
        self.max_quote_calls=max_quote_calls
        self._provider_dispatch_state=None
        self.daily_claim = json.loads(canonical(daily_claim))
        self.publication_binding = json.loads(canonical(publication_binding))
        self.validate_publication = validate_publication
        self.journal, self.settings, self.client = journal, settings, client
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._validate_daily()
        revisions = journal.membership_revisions_in_effective_order()
        self._normalization_cache = OrderedDict()
        self.normalization_cache_hits = self.normalization_cache_misses = 0
        self.verification_seconds = self.normalization_seconds = 0.0
        self.verification_reads = 0
        self._verified_history = None
        self._reader_view = None
        self._raw_history = None
        self.history_reused_events = 0
        self.path = journal.database_path.resolve().with_suffix('.hot.poller.sqlite3')
        self.lock_path = self.path.with_suffix('.lock')
        self.identity = {'contract': CONTRACT, 'journal': str(journal.database_path.resolve()),
            'baseline_sha256': revisions[0].content_sha256, 'settings': settings.payload(),
            'max_quote_calls':self.max_quote_calls,
            'payload_hashing':'stored-json-envelope-v1',
            'daily_binding_sha256': self.daily_binding['binding_sha256'],
            'daily_claim': self.daily_claim, 'publication_binding': self.publication_binding}
        self.run = PollRunProvenance('daily-hot-'+uuid4().hex, self.clock(), CONTRACT,
            {**settings.payload(), 'daily_hot_contract': CONTRACT,
             'parent_cadence_profile': 'interleaved-v1',
             'daily_binding_sha256': self.daily_binding['binding_sha256']})

    def _validate_daily(self):
        from .watchlist_polling import ET
        if hasattr(self,'identity') and (type(self.max_quote_calls) is not int or self.max_quote_calls!=self.identity['max_quote_calls']):
            raise ValueError('Daily native quote-call ceiling changed')
        b, claim = self.daily_binding, self.daily_claim
        content = dict(b); sha = content.pop('binding_sha256')
        path = self.journal.database_path.resolve()
        state = Path(b['settings']['state_root'])
        day = self.journal.session_date
        start = datetime.combine(day, datetime.min.time().replace(hour=9, minute=30), ET)
        stop = start.replace(hour=16)
        if (sha != digest(content) or b['contract'] != 'daily-hot-publication-session-v1'
                or claim['binding_sha256'] != sha or claim['state_root'] != str(state)
                or claim['contract'] != b['contract'] or self.journal.schema_version != 2
                or b['settings']['journal_path'] != str(path) or b['session_date'] != str(day)
                or path.name != f'{day}.sqlite3'
                or path.parent.name != f'quote_observation_journal_v2_api_opening_{day}'
                or state != state.resolve() or not state.name.startswith('daily_hot_')
                or state == path.parent or state in path.parents or path.parent in state.parents
                or self.settings.channel is not SamplingChannel.HOT
                or self.settings.seconds != RESERVED_HOT_SECONDS
                or not start <= self.settings.window.start_at < self.settings.window.end_at <= stop
                or self.settings.batch_size != 400 or self.settings.fields != 'all'
                or self.settings.source_label != 'daily-native-hot-v1'
                or self.settings.max_dispatch_lateness_seconds != .75
                or self.settings.retry_seconds != .05
                or self.publication_binding['journal'] != {
                    'path':str(path), 'schema_version':2, 'session_date':str(day)}):
            raise ValueError('Daily acquisition session, cadence, window or publication differs')
        expected = {'claim':claim, 'binding':b, 'status':'ATTACHED'}
        for filename, value in ((path.with_suffix('.daily-hot-session.json'), claim),
                                (state/'session.json', expected)):
            if (filename.is_symlink() or not filename.is_file() or filename.stat().st_size > 2_000_000
                    or json.loads(filename.read_text()) != value):
                raise ValueError('Restore the original complete daily publication binding')
        revisions = self.journal.membership_revisions_in_effective_order()
        if [r.content_sha256 for r in revisions[:2]] != b['published_anchor']:
            raise ValueError('Daily opening/Focus anchor changed')
        check_before_credentials(path, 'interleaved-v1')
        if self.validate_publication(None) is not True:
            raise ValueError('Publication callback did not verify the daily binding')

    def initialize(self):
        self._validate_daily()
        # The sidecar's initializer checks identity and existing channel rows.
        # Also refuse an abandoned foreign Hot registration before first claim.
        if not self.path.exists():
            with closing(sqlite3.connect(self.journal.database_path.resolve().as_uri()+'?mode=ro', uri=True)) as c:
                if any(json.loads(r[0]).get('watchlist_kind') == 'hot' for r in
                       c.execute('SELECT configuration_json FROM poll_run')):
                    raise ValueError('Restore the original Hot acquisition ledger before another run')
        return super().initialize()

    def _read_checked(self, connection):
        self._validate_daily()
        from .daily_acquisition_reader import read_daily_history
        return read_daily_history(self,connection)

    def _receipt(self,pending,*,connection=None):
        view=self._reader_view
        if view is None:return super()._receipt(pending,connection=connection)
        from .daily_acquisition_reader import receipt_from_view
        return receipt_from_view(self,pending,view)

    def sample(self,slot,*,fault_hook=None):
        from .daily_acquisition_cycle import sample_daily
        return sample_daily(self,slot,fault_hook=fault_hook)

    def provider_dispatch_state(self):
        state=self._provider_dispatch_state
        if state is None or state['pending'] is None:
            raise ValueError('Provider work requires the current verified native dispatch')
        return state

    def close_read_cache(self):
        if self._raw_history is not None:
            self._raw_history.close();self._raw_history=None
        self._verified_history=None

    def initialize_and_recover(self):
        """One checked existing-state pass under the acquisition owner lock.

        No live evidence is trusted from before credentials. A duplicate
        no-op snapshot after recording this new run would repeat that same
        check; the first native tick checks the current bytes again.
        """
        if not self.path.exists():
            self.initialize()
            return self.recover()
        from .sampling_acquisition import owner_lock
        self._validate_daily()
        with owner_lock(self.lock_path):
            state=self._read()
            self.journal.record_run(self.run)
            return self._recover(state)
