"""In-memory raw-value mirror; every persisted value is compared by SQLite.

Only the execution of comparisons moves into SQLite. Each read still visits
every scoped old row/column, rejects edits/deletions, and fetches every new row.
The mirror is derived, process-local, and never persisted or used for semantic
validation. The normal daily reader validates all new outcomes as before.
"""
import sqlite3
import os

SCOPED_DAILY_HISTORY_SUPPORTED = True


def quoted(value):
    return '"'+value.replace('"','""')+'"'


class RawHistoryMirror:
    def __init__(self,poller):
        self.paths=(poller.path.resolve(),poller.journal.database_path.resolve())
        self.file_identities=tuple(self._file_identity(path) for path in self.paths)
        self.connection=sqlite3.connect(':memory:',uri=True)
        self.connection.row_factory=sqlite3.Row
        self.connection.execute('PRAGMA temp_store=MEMORY')
        self.connection.execute('ATTACH DATABASE ? AS source',
            (poller.path.resolve().as_uri()+'?mode=ro',))
        self.connection.execute('ATTACH DATABASE ? AS journal',
            (poller.journal.database_path.resolve().as_uri()+'?mode=ro',))
        self.session,self.channel=str(poller.journal.session_date),poller.settings.channel.value
        scope=getattr(poller,'history_scope',None)
        if scope is not None:
            from .quote_observation_store import _utc_text
            self.window=tuple(_utc_text(at,'history_scope') for at in scope)
        else:self.window=None
        self.tables={}

    @staticmethod
    def _file_identity(path):
        value=os.stat(path,follow_symlinks=False)
        return value.st_dev,value.st_ino,value.st_mode

    def _files_unchanged(self):
        # This fences path/handle replacement only. Contents are still fully
        # compared below on every call, independently of timestamps and sizes.
        if tuple(self._file_identity(path) for path in self.paths)!=self.file_identities:
            raise ValueError('Previously verified history file was replaced')

    def close(self):
        self.connection.close()

    def _rows(self,name,schema,table,scope,params):
        c=self.connection
        columns=tuple(row['name'] for row in c.execute('PRAGMA '+schema+'.table_info('+quoted(table)+')'))
        selected=','.join('a.'+quoted(col) for col in columns)
        old=self.tables.get(name)
        if old is not None:
            if columns!=old['columns']:raise ValueError('Previously verified history schema changed')
            different=' OR '.join('a.'+quoted(col)+' IS NOT old.'+quoted(col) for col in columns)
            if c.execute('SELECT 1 FROM '+quoted(name)+' old LEFT JOIN '+schema+'.'+quoted(table)+
                    ' a ON a.rowid=old._source_rowid WHERE a.rowid IS NULL OR '+different+' LIMIT 1').fetchone():
                label='acquisition prefix' if name=='raw_events' else 'journal values'
                raise ValueError('Previously verified '+label+' changed')
            where=' AND a.rowid NOT IN (SELECT _source_rowid FROM '+quoted(name)+')'
        else:where=''
        statement='SELECT a.rowid AS _source_rowid,'+selected+' FROM '+scope+where+' ORDER BY a.rowid'
        # Scope supplies a WHERE clause, with a named alias for the actual row.
        new=c.execute(statement,params).fetchall()
        if old is None:
            c.execute('CREATE TABLE '+quoted(name)+' AS '+statement,params)
            c.execute('CREATE UNIQUE INDEX '+quoted(name+'_rowid')+' ON '+quoted(name)+'(_source_rowid)')
            old={'columns':columns,'rows':(),'named':(),'last_rowid':0};self.tables[name]=old
        elif new:
            c.executemany('INSERT INTO '+quoted(name)+' VALUES('+','.join('?' for _ in range(len(columns)+1))+')',
                (tuple(row) for row in new))
        # Tuple values retain exact SQLite types. Header lookup uses the
        # original column names; observations/events use original positions.
        if new:
            if new[0][0]<=old['last_rowid']:
                raise ValueError('Previously verified history order changed')
            values=tuple(tuple(row)[1:] for row in new)
            old['rows']+=values
            if name in ('raw_headers','raw_skips'):
                old['named']+=tuple(dict(zip(columns,row)) for row in values)
            old['last_rowid']=new[-1][0]
        return old

    def read(self,identity):
        c=self.connection
        self._files_unchanged()
        c.execute('BEGIN')
        try:
            row=c.execute('SELECT payload FROM source.acquisition_identity WHERE id=1').fetchone()
            if row is None or row[0]!=identity:raise ValueError('Acquisition ledger identity/settings mismatch')
            params=(self.session,self.channel)
            header_window=observation_window=skip_window=''
            if self.window is not None:
                header_window=skip_window=' AND a.scheduled_at_utc>=? AND a.scheduled_at_utc<?'
                observation_window=' AND h.scheduled_at_utc>=? AND h.scheduled_at_utc<?'
                params+=self.window
            headers=self._rows('raw_headers','journal','quote_acquisition',
                'journal.quote_acquisition a WHERE a.session_date=? AND a.channel=?'+header_window,params)
            observations=self._rows('raw_observations','journal','quote_observation',
                'journal.quote_acquisition h INDEXED BY quote_acquisition_session_channel_idx '
                'CROSS JOIN journal.quote_observation a WHERE a.acquisition_id=h.acquisition_id '
                'AND h.session_date=? AND h.channel=?'+observation_window,params)
            skips=self._rows('raw_skips','journal','poll_slot_skip',
                'journal.poll_slot_skip a WHERE a.session_date=? AND a.channel=?'+skip_window,params)
            events=self._rows('raw_events','source','acquisition_event',
                'source.acquisition_event a WHERE 1=1',())
            self._files_unchanged()
            c.commit()
            return {'headers':headers['named'],'raw_headers':headers['rows'],
                'observations':observations['rows'],'skips':skips['named'],
                'raw_skips':skips['rows'],'events':events['rows']}
        except BaseException:
            c.rollback()
            # A failed comparison must never leave partly updated derived
            # state available to the next read.
            self.tables.clear()
            for name in ('raw_headers','raw_observations','raw_skips','raw_events'):
                c.execute('DROP TABLE IF EXISTS '+quoted(name))
            c.commit()
            raise
