# Daily Universe Production Runbook

## Purpose

This procedure builds the stable opening `Uni` roster for the next trading
session. It freezes four evidence layers:

1. the current Nasdaq Trader symbol directory;
2. one Schwab post-close quote snapshot for every non-ETF, non-test candidate;
3. one deterministic inclusion or rejection decision for every directory
   symbol;
4. one strict schema-v2 opening hierarchy `r0` proposal.

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

Run the workflow after 16:00 Eastern Time on the completed trading day and
before the Eastern calendar date changes. Version 1 uses Schwab
`quote.totalVolume`, whose current-session meaning rolls forward before the
next morning. A missed snapshot must not be reconstructed the following
morning from that field. The operator supplies both dates explicitly:

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
3. `build_daily_universe.py`;
4. `build_opening_sampling_hierarchy.py`.

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

opening_hierarchy_r0.json
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

### Artifact path contract

Two output layouts are valid because the stages remain independently runnable:

| Producer | Universe artifact directory | Opening `r0` proposal |
|---|---|---|
| Preferred `run_daily_universe_production.py` workflow | `output\daily_universe_production\TARGET-from-SESSION\universe` | `output\daily_universe_production\TARGET-from-SESSION\opening_hierarchy_r0.json` |
| Standalone `build_daily_universe.py` followed by `build_opening_sampling_hierarchy.py` | `output\daily_universe\TARGET-from-SESSION` | `output\daily_universe\TARGET-from-SESSION\opening_hierarchy_r0.json` |

The accepted September 21 opening artifacts use the standalone layout under
`output\daily_universe\2026-09-21-from-2026-09-18`. Its `uni_symbols.csv`,
`uni_watchlist.csv`, `decision_ledger.csv`, and `manifest.json` are directly in
that directory; there is no intermediate `universe` directory.

Downstream validation, API OV acquisition, Focus production, and publication must
consume the accepted artifact paths from the producer that actually ran. Do
not reconstruct a different path from the dates, and do not move or duplicate
immutable evidence merely to make it resemble the other layout. The maintained
operator commands in `docs\Operations_Quick_Reference.md` use `UNIVERSE_DIR`
and `OPENING_R0` for this handoff.

## Acceptance checks

Before using the roster, confirm:

1. all four stages and the top-level workflow report `PASS`;
2. the snapshot status and session-match counts are printed and plausible;
3. the universe build reports nonzero included symbols;
4. the rejection reason counts are present;
5. `opening_hierarchy_r0.json` and `workflow_manifest.json` exist;
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

### Legacy schema-v1 fallback

Without explicit publication, pass the generated Watchlist directly to the
static schema-v1 Uni poller:

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

This remains available for compatibility and controlled fallback. Do not run
it alongside the schema-v2 Uni poller for the same session.

### Explicit schema-v2 opening publication

To publish the generated opening hierarchy as `r0`, add an explicit journal
root to the production command:

```cmd
python probes\run_daily_universe_production.py ^
  --session-date 2026-09-18 ^
  --target-date 2026-09-21 ^
  --publish-journal-root output\quote_observation_journal_v2
```

This adds a fifth stage. It immediately creates the target session's database
and atomically publishes `r0`, effective at 09:30 ET. Opening membership is:

- Uni: every included daily-universe symbol;
- Focus: empty, awaiting the pre-open OV `BASE_SET`;
- Hot: empty, awaiting Focus selection.

Publication must complete before the target session's 09:30 ET effective
time. The publisher fails closed rather than backdating a membership revision.

To create and publish `r0` from an already completed standalone universe build
without reacquiring market data:

```cmd
python probes\build_opening_sampling_hierarchy.py ^
  "output\daily_universe\2026-09-21-from-2026-09-18" ^
  --output "output\daily_universe\2026-09-21-from-2026-09-18\opening_hierarchy_r0.json"

python probes\publish_sampling_hierarchy.py ^
  "output\quote_observation_journal_v2\2026-09-21.sqlite3" ^
  "output\daily_universe\2026-09-21-from-2026-09-18\opening_hierarchy_r0.json"
```

The corresponding schema-v2 Uni poller reads membership from the journal and
does not accept `--universe-csv`:

```cmd
python probes\probe_universe_quote_watch.py ^
  --watchlist-kind uni ^
  --start-at 2026-09-21T09:30:00-04:00 ^
  --stop-at 2026-09-21T16:00:00-04:00 ^
  --journal-root output\quote_observation_journal_v2 ^
  --journal-schema-version 2
```

Do not run schema-v1 pollers against a journal root where the target day's
database has been initialized as schema v2. Use a dedicated root for a
controlled transition until schema-v2 operation is formally adopted.

Before the open, `acquire_api_overnight_volume.py` calculates `OV_DECISION`
for every opening-Uni symbol from Schwab five-minute extended-hours candles in
the half-open `00:00–09:00 ET` decision window. The immutable API bundle is
then consumed by `schwab_watchlists` to produce the strict Focus `r1`
proposal. Publication remains a separate acceptance step. ToS is optional
outbound display only; no ToS CSV export or readback is part of this workflow.
The timed command sequence, r1 validation/publication, and both schema-v2
pollers are maintained in `docs\Operations_Quick_Reference.md`.

## Failure behavior and recovery

- Before 16:00 ET on `--session-date`, same-day snapshot acquisition fails.
- After the ET calendar date advances beyond `--session-date`, acquisition
  also fails because `quote.totalVolume` is no longer accepted as the
  completed session's volume evidence.
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
it deliberately retains three operator controls:

- the operator chooses the session and target trading dates;
- Schwab authentication may require an interactive encrypted-config password;
- schema-v2 journal publication requires the explicit
  `--publish-journal-root` option;
- the operator explicitly chooses the OV Focus size and accepts its `r1`
  decision ledger before publication.

Current-day opening `OV_DECISION` is now MasterBot-derived and independent of
ToS. Historical 3/5/10/30-session OV features, journal distillation, and
long-term database ingestion remain downstream work.
