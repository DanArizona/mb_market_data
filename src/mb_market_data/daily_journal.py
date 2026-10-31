"""Daily-only membership reads with complete persisted-value comparison.

Reuse immutable parsed revisions only after rereading every header and member
value in a consistent SQLite read transaction. No head/mtime trust or disk cache.
The regular QuoteObservationStore remains the reference parser.
"""
from collections import OrderedDict

from .quote_observation_store import QuoteObservationStore, _utc_text, _require_aware


class DailyQuoteObservationStore(QuoteObservationStore):
    def _membership_revision_from_row(self,connection,row):
        key=(row['session_date'],row['revision'])
        channels=connection.execute('SELECT * FROM sampling_channel_revision '
            'WHERE session_date=? AND revision=? ORDER BY channel',key).fetchall()
        members=connection.execute('SELECT * FROM sampling_channel_member '
            'WHERE session_date=? AND revision=? ORDER BY channel,ordinal',key).fetchall()
        raw=(tuple(row),tuple(tuple(x) for x in channels),tuple(tuple(x) for x in members))
        if not hasattr(self,'_daily_revision_cache'):self._daily_revision_cache=OrderedDict()
        old=self._daily_revision_cache.get(key)
        if old is not None and old[0]==raw:
            self._daily_revision_cache.move_to_end(key)
            return old[1]
        parsed=super()._membership_revision_from_row(connection,row)
        self._daily_revision_cache[key]=(raw,parsed)
        self._daily_revision_cache.move_to_end(key)
        if len(self._daily_revision_cache)>128:self._daily_revision_cache.popitem(last=False)
        return parsed

    def membership_revisions_in_effective_order(self):
        self._require_schema_version(2)
        with self._connection() as connection:
            connection.execute('BEGIN')
            rows=connection.execute('SELECT * FROM sampling_membership_revision '
                'ORDER BY effective_at_utc,revision').fetchall()
            return tuple(self._membership_revision_from_row(connection,row) for row in rows)

    def latest_membership_revision_effective_at(self,value):
        self._require_schema_version(2)
        _require_aware(value,'value')
        with self._connection() as connection:
            connection.execute('BEGIN')
            row=connection.execute('SELECT * FROM sampling_membership_revision '
                'WHERE session_date=? AND effective_at_utc<=? '
                'ORDER BY effective_at_utc DESC,revision DESC LIMIT 1',
                (str(self.session_date),_utc_text(value,'value'))).fetchone()
            return self._membership_revision_from_row(connection,row) if row is not None else None
