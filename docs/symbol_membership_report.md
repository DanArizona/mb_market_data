# Daily symbol membership CLI

Report effective Uni, Focus and Hot membership changes and recorded reasons from
one saved daily quote journal and its matching selection evidence.
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

Text reports each symbol's joins, departures and reentries, with the effective
time, recorded reason, revision and source. It includes YES/NO at the inclusive
cutoff for channels the symbol joined. Unchanged membership is not repeated.

For a schema-v2 symbol that never joined Uni, the report prints just:

```text
UNI: never a member | <recorded exclusion reason>
```

It omits Focus and Hot because the validated hierarchy makes both impossible.
If Uni was joined but Focus was never joined, it reports Uni and Focus and
omits Hot. Legacy schema-v1 channels are reported independently because that
schema does not prove the nested contract. Missing history stays NULL, and
membership before the first recorded revision remains unknown. Thus "never"
means never in recorded history through the requested cutoff.

JSON (`--format json`, contract `daily-symbol-membership-report-v2`) retains all
three channels, intervals and raw revision history and adds `changes`, their
`explanation` objects, metadata and `never_reason` for never-member channels.
It exposes effective and schema-v2 publication timestamps. Intervals end when
the next state starts; `at_cutoff` includes a change exactly at cutoff. Recorded
states at the same effective time are preserved in revision order.

### Selection reasons

Uni reasons come from the saved `decision_ledger.csv`; Focus reasons come from
`focus_decision_ledger.csv`. The exact SHA-256 recorded in the published
selector metadata must match, and the ledger's inclusion set must equal that
channel's published membership. An unrelated, newer, altered or merely similarly
named ledger is never substituted. Unchanged Uni history on a later Focus
revision still uses the Uni selector's original evidence.

Automatic search locations are the repository's
`output/daily_universe_production` and the sibling `schwab_watchlists` repository's
`output/ov_focus_production` (also `output/ov_focus_production` in the selected
market-data root). Use repeatable `--evidence-root PATH` for a preserved archive
elsewhere. This is a read-only search for the two ledger filenames, not a
discovery or merger of other journals.

Carry-forward metadata can retain nested original selector metadata; its input
session date is shown rather than labeling it as fresh data for the requested
date. No Monday exclusion reason is inferred from Monday's post-close ledger
for Tuesday. Reason codes, captured market cap/close/volume and Focus OV/rank/
selection limit are shown when available. Missing, unreadable or unbound reason
evidence produces an explicit `reason unavailable` while valid membership is
still reported. Journal-only change reasons are labeled `revision reason` so
they are not mistaken for symbol-specific screening evidence. Absence from Hot
does not prove that no request was submitted or why one was rejected.

- No default journal, or no membership revisions effective by cutoff: each
  symbol's entire report is `NULL` (`null` in JSON).
- Valid membership history with a symbol absent: **never a member**, a non-null
  report. That is different from missing daily data or an unavailable reason.
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
tables, including committed WAL data, and optionally matching decision ledgers;
quote rows and raw acquisition evidence are not scanned. A prepared future-day journal describes its recorded plan; it
does not establish that those pollers ran.

The standalone delivery contains the same module as `membership_report.py`
and `report_membership.cmd`. Extract its folder under `mb_market_data\output`
and call the CMD wrapper. It passes the repository root explicitly without
changing the current directory. No production source replacement is needed.

Validation: `PYTHONPATH=src python -m unittest discover -s tests -p 'test_membership_report*.py'`.
Tests create native schema-v1/v2 journals and saved decision ledgers, exercise
changes/cutoffs/missing data/errors, child suppression, original-source binding,
carry-forward metadata, concurrent WAL reads and byte-preserving read-only
reports, and execute the CLI in a subprocess.
Production symbol results still require running against the operator's local
daily journal; synthetic results are never presented as production evidence.

Membership case studies awaiting review are tracked in the
[dated investigation register](research/membership_criteria_cases.md).
