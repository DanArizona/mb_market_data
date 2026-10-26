# Daily symbol membership CLI

Report effective Uni, Focus and Hot membership from one saved daily quote journal.
Read-only SQLite snapshot, no password, API requests, poller changes or installation.
Membership is independent of quote validity and polling health.

From the mb_market_data repository root:

```cmd
python "probes\report_symbol_membership.py" "AZTR CLRO LUCD JAGX NN SAIQ" --date 2026-10-26
```

Omit `--date` for today **in ET**. Today defaults to the earlier of now and
16:00 ET. Other dates default to 16:00 ET. Override with `--end-time 12:15`
or `--end-time 12:15:30`; all input and output times use America/New_York.
An explicit end time may be after 16:00. Repeated/missing DST wall times are
rejected rather than guessed.

The default source is exactly:

`<market-data-root>\output\quote_observation_journal_v2_api_opening_<date>\<date>.sqlite3`

`--market-data-root` defaults to the current directory. `--database` selects
one explicit journal, including schema-v1 historical data or an isolated
experiment. The default never discovers or merges experiment journals. It
does not fall back to a universe proposal or watchlist file that might not
have been published.

Text lists membership intervals and YES/NO at the inclusive cutoff. JSON
(`--format json`) also provides unknown/nonmember intervals, revision,
effective time, schema-v2 publication time, source and reason. Unchanged
membership across revisions is coalesced. Intervals end when the next state
starts; the separate `at_cutoff` value includes a change exactly at cutoff.

- No default journal, or no membership revisions effective by cutoff: each
  symbol's entire report is `NULL` (`null` in JSON).
- Valid membership history with a symbol absent: **NO / never in recorded
  history**, a non-null report. That is different from missing daily data.
- Before the first revision: **unknown**, never assumed to be nonmember.
- Schema-v1 channels without history: per-channel `NULL`.
- Mistyped explicit path, unreadable/unsupported database, wrong date,
  incomplete hierarchy, count or nesting mismatch: **ERROR**, exit code 2.
  A valid report or genuine no-data result returns 0.

This is effective history reconstructed from revisions saved **now**, not an
as-known-then replay. JSON exposes publication and effective timestamps;
schema-v2 publication must not follow its effective time. It checks
membership structure and uses one read transaction, but does not
replace the full journal integrity/hash/acquisition audit. It reads membership
tables only, including committed WAL data; quote rows and raw evidence are
not scanned. A prepared future-day journal describes its recorded plan; it
does not establish that those pollers ran.

The standalone delivery contains the same module as `membership_report.py`
and `report_membership.cmd`. Extract its folder under `mb_market_data\output`
and call the CMD wrapper. It passes the repository root explicitly without
changing the current directory. No production source replacement is needed.

Validation: `python -m unittest discover -s tests -p test_membership_report.py`.
Tests create native schema-v1/v2 journals, exercise changes/cutoffs/missing
data/errors and concurrent WAL reads, and execute the CLI in a subprocess.
Production symbol results still require running against the operator's local
daily journal; synthetic results are never presented as production evidence.
