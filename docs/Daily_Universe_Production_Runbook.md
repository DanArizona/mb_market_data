# Daily Universe Production Runbook

## Purpose

This procedure builds the stable opening `Uni` roster for the next trading
session. It freezes three evidence layers:

1. the current Nasdaq Trader symbol directory;
2. one Schwab post-close quote snapshot for every non-ETF, non-test candidate;
3. one deterministic inclusion or rejection decision for every directory
   symbol.

The resulting roster is an opening-session input. Intraday additions and the
future `Hot ⊆ Focus ⊆ Uni` membership workflow are separate concerns.

## Version 1 selection policy

The selector uses the just-completed regular session:

- close: Schwab `regular.regularMarketLastPrice`;
- volume: Schwab `quote.totalVolume`;
- shares: Schwab `fundamental.sharesOutstanding`;
- calculated market capitalization: regular close times shares outstanding.

The inclusive default limits are:

- volume at least 10,000 shares;
- close at least $0.10;
- calculated market capitalization from $4,000,000 through $40,000,000;
- ETF is not `Y`;
- test issue is not `Y`;
- regular-market trade date matches the completed session.

Missing or stale required data fails closed and is recorded in the decision
ledger. Nasdaq financial status is preserved as evidence but is not currently
an exclusion rule.

## Timing and dates

Run the workflow after 16:00 Eastern Time on the completed trading day. The
operator supplies both dates explicitly:

- `--session-date`: the completed regular trading session;
- `--target-date`: the next intended trading session.

Version 1 intentionally does not guess the exchange calendar. Confirm the
target date across weekends and market holidays before running.

## Preferred production command

Run from the `mb_market_data` repository with the project environment active.
For the Friday, September 18 to Monday, September 21 example:

```cmd
python probes\run_daily_universe_production.py ^
  --session-date 2026-09-18 ^
  --target-date 2026-09-21
```

The command performs, in order:

1. `probe_nasdaq_symbol_directory.py`;
2. `acquire_daily_universe_snapshot.py`;
3. `build_daily_universe.py`.

It prompts once for the encrypted Schwab configuration password during the
second stage. A successful run ends with:

```text
Daily universe production: PASS
```

To inspect the exact commands without network access, credentials, or writes:

```cmd
python probes\run_daily_universe_production.py ^
  --session-date 2026-09-18 ^
  --target-date 2026-09-21 ^
  --plan
```

## Output layout

The default workflow directory is:

```text
output\daily_universe_production\TARGET-from-SESSION
```

It contains:

```text
symbol_directory\
    nasdaqlisted.txt
    otherlisted.txt
    normalized_all.csv
    candidate_non_etf_non_test.csv
    manifest.json

market_snapshot\
    market_data_snapshot.csv
    quote_acquisition.json
    manifest.json

universe\
    decision_ledger.csv
    uni_watchlist.csv
    uni_symbols.csv
    manifest.json

workflow_manifest.json
```

All directories are immutable: the workflow refuses to overwrite an existing
run. Each manifest records hashes and provenance. If an intentional rerun is
required, preserve the first attempt and provide a distinct directory:

```cmd
python probes\run_daily_universe_production.py ^
  --session-date 2026-09-18 ^
  --target-date 2026-09-21 ^
  --output-dir "output\daily_universe_production\2026-09-21-from-2026-09-18-r2"
```

## Acceptance checks

Before using the roster, confirm:

1. all three stages and the top-level workflow report `PASS`;
2. the snapshot status and session-match counts are printed and plausible;
3. the universe build reports nonzero included symbols;
4. the rejection reason counts are present;
5. `workflow_manifest.json` exists;
6. a motivating or boundary symbol can be explained from
   `universe\decision_ledger.csv`.

For example:

```cmd
powershell -NoProfile -Command "Import-Csv 'output\daily_universe_production\2026-09-21-from-2026-09-18\universe\decision_ledger.csv' | Where-Object symbol -eq 'DAIC' | Format-List *"
```

The September 18 controlled production-style build selected 554 symbols for
September 21. DAIC was included from a $3.55 regular close, 4,105,261 shares of
volume, 1,210,383 shares outstanding, and a calculated market capitalization
of $4,296,859.65.

## Starting the next session

Until schema-v2 opening-membership publication is integrated, pass the
generated Watchlist directly to the static schema-v1 Uni poller:

```cmd
python probes\probe_universe_quote_watch.py ^
  --universe-csv "output\daily_universe_production\2026-09-21-from-2026-09-18\universe\uni_watchlist.csv" ^
  --watchlist-kind uni ^
  --watchlist-revision 0 ^
  --start-at 2026-09-21T09:30:00-04:00 ^
  --stop-at 2026-09-21T16:00:00-04:00 ^
  --journal-root output\quote_observation_journal ^
  --journal-schema-version 1
```

Run Focus independently in its existing process. Both processes may share the
same daily schema-v1 journal.

## Failure behavior and recovery

- Before 16:00 ET on `--session-date`, same-day snapshot acquisition fails.
- A stage failure stops the workflow; later stages do not run.
- Partial output is retained as evidence and is never silently overwritten.
- Invalid or unavailable Schwab symbols remain explicit snapshot and decision
  rows.
- A regular trade timestamp from another session is rejected rather than
  silently treated as current.
- Missing shares outstanding prevents market-cap validation and therefore
  rejects the symbol.

Review the failing stage's console output and manifest. Correct the external
or configuration problem, then use a new `--output-dir`. Individual stage
probes remain available for diagnosis.

## Current production boundary

This version provides a repeatable, version-controlled, one-command build, but
it deliberately retains three manual controls:

- the operator chooses the session and target trading dates;
- Schwab authentication may require an interactive encrypted-config password;
- the generated opening roster is passed manually to the schema-v1 poller.

The next integration step is to publish the roster as the target session's
schema-v2 hierarchical membership revision `r0`. Historical journal
distillation and long-term database ingestion are separate downstream work.
