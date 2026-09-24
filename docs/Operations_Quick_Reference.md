# mb_market_data Operations Quick Reference

**Purpose:** A concise operator manual for routine `mb_market_data` work.  
**Last verified:** 2026-09-23.
**Shell:** Windows Command Prompt (`cmd.exe`).  
**Working directory:** `C:\Users\danla\Documents\github\mb_market_data`.

This is the maintained starting point for day-to-day commands. Detailed design
documents and runbooks remain authoritative for implementation details, but an
operator should be able to run and validate the normal workflow from this
document.

The current edition covers `mb_market_data`, the API-only OV-to-Focus path in
`schwab_watchlists`, and the directly required `mb_tools` scanner and
Schwab-auth commands.

## 1. Rules that prevent the expensive mistakes

1. Run commands from the repository root with the `sea-green` environment
   active.
2. All market-session times are Eastern Time. Always include the correct UTC
   offset in scheduled timestamps: `-04:00` during EDT or `-05:00` during EST.
3. Confirm `SESSION_DATE` and `TARGET_DATE` manually across weekends and market
   holidays. The workflow does not yet use an exchange calendar.
4. Run the daily-universe workflow only after 16:00 ET on `SESSION_DATE`.
5. Never mix schema-v1 and schema-v2 pollers in the same journal root for the
   same session date.
6. Do not run both the old static Uni poller and the schema-v2 Uni poller for
   the same session. That duplicates the Schwab requests.
7. Schema-v2 opening revisions `r0` and `r1` must be published before their
   09:30 ET effective time. The publisher rejects backdating.
8. Do not put an active SQLite journal or its WAL files on a network share.
9. Preserve completed output directories and journals. Production artifacts
   are immutable evidence; use a new suffixed output directory for an
   intentional rerun.
10. ToS is display-only. Roster submission is outbound and unverified: do not
    export a ToS CSV, read ToS membership back, or use ToS values in a market-
    data decision. Command acceptance proves only that the adapter accepted
    the request.

## 2. Start a command window

```cmd
cd /d C:\Users\danla\Documents\github\mb_market_data
conda activate sea-green
git status --short
git pull --ff-only origin main
```

If `git status --short` reports local or untracked files, identify and preserve
them before pulling. Do not delete an unfamiliar file merely to make a pull
succeed.

Optional code-health check after pulling:

```cmd
python -m pytest -q tests
```

Before a polling session, inspect the stored Schwab authorization lifetime:

```cmd
mb-schwab-auth --status
```

If authorization maintenance is required, complete it before starting either
poller:

```cmd
mb-schwab-auth
```

Exact-slot pollers also perform a read-only credential preflight. They fail
before creating output or registering a journal run if the stored refresh
horizon is insufficient for the entire polling window.

## 3. Set the session values

Set these once in each command window. The values below are an example; replace
them for the session being operated.

```cmd
set SESSION_DATE=2026-09-18
set TARGET_DATE=2026-09-21
set ET_UTC_OFFSET=-04:00
set MARKET_DATA_ROOT=C:\Users\danla\Documents\github\mb_market_data
set WATCHLIST_ROOT=C:\Users\danla\Documents\github\schwab_watchlists
set UNIVERSE_DIR=%MARKET_DATA_ROOT%\output\daily_universe_production\%TARGET_DATE%-from-%SESSION_DATE%\universe
set OPENING_R0=%MARKET_DATA_ROOT%\output\daily_universe_production\%TARGET_DATE%-from-%SESSION_DATE%\opening_hierarchy_r0.json
set API_OV_ROOT=%MARKET_DATA_ROOT%\output\api_overnight_volume
set V2_JOURNAL_ROOT=output\quote_observation_journal_v2
set FOCUS_LIMIT=40
```

Definitions:

| Variable | Meaning |
|---|---|
| `SESSION_DATE` | The just-completed regular trading session used to select Uni. |
| `TARGET_DATE` | The next intended trading session. |
| `ET_UTC_OFFSET` | Eastern offset on `TARGET_DATE`, including daylight-saving time. |
| `UNIVERSE_DIR` | Accepted directory that directly contains `uni_symbols.csv`, `uni_watchlist.csv`, `decision_ledger.csv`, and the universe `manifest.json`. |
| `OPENING_R0` | Exact accepted `opening_hierarchy_r0.json` proposal paired with `UNIVERSE_DIR`. |
| `API_OV_ROOT` | Root for immutable API-only OV acquisition bundles. |
| `V2_JOURNAL_ROOT` | A dedicated schema-v2 journal root. Use a purpose/date suffix for a controlled test. |
| `MARKET_DATA_ROOT` | Local `mb_market_data` repository root. |
| `WATCHLIST_ROOT` | Local `schwab_watchlists` repository root. |
| `FOCUS_LIMIT` | Explicit maximum size of the OV-derived opening Focus set. `40` is an example, not a hidden project default. |

The default `UNIVERSE_DIR` and `OPENING_R0` values above match the preferred
one-command production workflow. If an explicit `--output-dir` or independently
run stage produced the accepted artifacts, reset both variables to those
actual paths before validation or publication.

For the controlled September 21 opening test, the accepted artifacts came
from the standalone build layout and the journal used a dedicated root:

```cmd
set UNIVERSE_DIR=%MARKET_DATA_ROOT%\output\daily_universe\2026-09-21-from-2026-09-18
set OPENING_R0=%MARKET_DATA_ROOT%\output\daily_universe\2026-09-21-from-2026-09-18\opening_hierarchy_r0.json
set V2_JOURNAL_ROOT=output\quote_observation_journal_v2_opening_2026-09-21
```

Do not move or copy accepted evidence to make one layout resemble the other.
The handoff contract is the directory that directly contains the accepted Uni
CSVs plus the exact paired opening proposal.

## 4. After the close: build the next session's Uni

### 4.1 Preview the workflow

This performs no network access and writes nothing:

```cmd
python probes\run_daily_universe_production.py ^
  --session-date %SESSION_DATE% ^
  --target-date %TARGET_DATE% ^
  --plan
```

Confirm that the dates, paths, thresholds, and four stages are correct.

### 4.2 Run the workflow

```cmd
python probes\run_daily_universe_production.py ^
  --session-date %SESSION_DATE% ^
  --target-date %TARGET_DATE%
```

Enter the encrypted Schwab configuration password when prompted. A successful
run reports `Daily universe production: PASS` and creates:

```text
output\daily_universe_production\TARGET_DATE-from-SESSION_DATE\
    symbol_directory\
    market_snapshot\
    universe\
        decision_ledger.csv
        uni_watchlist.csv
        uni_symbols.csv
        manifest.json
    opening_hierarchy_r0.json
    workflow_manifest.json
```

The four stages are:

1. acquire and normalize the Nasdaq symbol directory;
2. acquire one Schwab post-close snapshot;
3. build the deterministic Uni roster and decision ledger;
4. build the schema-v2 opening hierarchy proposal.

### 4.3 Validate the roster before publication

Confirm in the console output:

- all four stages report `PASS`;
- the included count is nonzero and plausible;
- status and session-match counts are present;
- rejection reason counts are present;
- Uni has the included count while opening Focus and Hot are zero.

Count the generated Uni roster:

```cmd
powershell -NoProfile -Command "$u=Import-Csv '%UNIVERSE_DIR%\uni_symbols.csv'; 'UNI COUNT: ' + @($u).Count"
```

Explain a particular symbol from the decision ledger:

```cmd
powershell -NoProfile -Command "Import-Csv '%UNIVERSE_DIR%\decision_ledger.csv' | Where-Object symbol -eq 'DAIC' | Format-List *"
```

Replace `DAIC` with the symbol under investigation. The ledger is the
authoritative answer to why a directory symbol was included or rejected.

### 4.4 Publish opening membership to schema v2

After accepting the roster, publish its already-generated `r0` proposal:

```cmd
python probes\publish_sampling_hierarchy.py ^
  "%V2_JOURNAL_ROOT%\%TARGET_DATE%.sqlite3" ^
  "%OPENING_R0%"
```

Expected result:

- `Result: inserted`;
- session date equals `TARGET_DATE`;
- revision is `r0`;
- effective time is 09:30 ET;
- Uni count matches the accepted roster;
- Focus and Hot are zero;
- the proposal and publication content hashes match.

The production wrapper also supports immediate fifth-stage publication with
`--publish-journal-root`. The separate command above is the recommended
transition procedure because it leaves an explicit human acceptance point
between roster construction and publication.

After schema-v2 operation is accepted as routine, the combined form is:

```cmd
python probes\run_daily_universe_production.py ^
  --session-date %SESSION_DATE% ^
  --target-date %TARGET_DATE% ^
  --publish-journal-root %V2_JOURNAL_ROOT%
```

### 4.5 Validate the unopened schema-v2 journal

```cmd
python probes\audit_quote_observation_journal.py ^
  "%V2_JOURNAL_ROOT%\%TARGET_DATE%.sqlite3" ^
  --show-missing 20
```

```cmd
python probes\replay_quote_observation_journal.py ^
  "%V2_JOURNAL_ROOT%\%TARGET_DATE%.sqlite3" ^
  --progress-every 0
```

At this point the expected state is one hierarchy event, no acquisitions, and
no observations. Replay should project `r0` with the accepted Uni count and
empty Focus/Hot.

## 5. Before the open: build and publish OV-derived Focus

This procedure produces the daily Focus `BASE_SET` as schema-v2 revision `r1`.
It preserves `Focus ⊆ Uni`. `OV_DECISION` is calculated only from Schwab
five-minute extended-hours candles whose starts satisfy
`00:00 <= candle start < 08:25 ET`. ToS is not an input.

### 5.1 Scheduled opening sequence

For a normal 09:30 ET open, begin about one hour early:

| ET | Action |
|---|---|
| 08:20 | Confirm both repositories are current, run `mb-schwab-auth --status`, verify `OPENING_R0`, journal path, dates, and Focus limit. |
| 08:25 | Start the complete opening-Uni API OV acquisition. Do not start earlier; the decision window has not closed. |
| About 08:30 | Confirm the API bundle reports zero failures and `production_eligible: true`. |
| 08:31 | Build and review the API-OV Focus r1 bundle. |
| 08:35 | Publish r1; audit and replay the journal. |
| 08:40 | Start Uni and Focus pollers with a 09:30 start time. |
| 08:45 or later | Optionally send the accepted Focus roster to ToS for display. Do not verify by export or readback. |
| 09:30 | Confirm both pollers begin on schedule. |

### 5.2 Acquire API-only `OV_DECISION`

From the `mb_market_data` repository root:

```cmd
cd /d %MARKET_DATA_ROOT%

python probes\acquire_api_overnight_volume.py ^
  --opening-proposal "%OPENING_R0%" ^
  --output-root "%API_OV_ROOT%"
```

Enter the encrypted Schwab configuration password when prompted. With 554
symbols and the default 0.5-second request interval, the theoretical pacing
minimum is about 4.6 minutes; retries can extend the run. The command writes:

```text
api_ov_observations.csv
api_ov_candles.jsonl
manifest.json
```

Set the printed manifest path explicitly:

```cmd
set API_OV_MANIFEST=%API_OV_ROOT%\RUN_TIMESTAMP-session-%TARGET_DATE%\manifest.json
```

Accept the bundle only when all opening-Uni symbols succeeded,
`complete_opening_uni` is true, `completed_before_opening` is true, and
`production_eligible` is true. A smoke bundle created with `--max-symbols`
can never be used for production Focus.

### 5.3 Build the Focus `BASE_SET` and `r1` proposal

Run this from the `schwab_watchlists` repository:

```cmd
cd /d %WATCHLIST_ROOT%
git pull --ff-only origin main

python run_ov_focus_production.py ^
  --api-ov-manifest "%API_OV_MANIFEST%" ^
  --opening-proposal "%OPENING_R0%" ^
  --limit %FOCUS_LIMIT%
```

This step does not authenticate to Schwab and does not read ToS. It verifies
the upstream hashes, exact opening-Uni roster, session, decision window, and
production eligibility before ranking. A successful run reports
`API OV Focus production: PASS` and writes:

```text
focus_decision_ledger.csv
focus_symbols.csv
sampling_hierarchy_r1.json
manifest.json
```

Copy the printed output directory into a variable. Example:

```cmd
set OV_R1_DIR=C:\Users\danla\Documents\github\schwab_watchlists\output\ov_focus_production\2026-09-21-08-25-30
```

Review the source, eligible, and selected counts and inspect the complete
decision-reason distribution:

```cmd
powershell -NoProfile -Command "Import-Csv '%OV_R1_DIR%\focus_decision_ledger.csv' | Group-Object primary_reason | Sort-Object Count -Descending | Format-Table Count,Name -AutoSize"
```

### 5.4 Publish and validate `r1`

Return to `mb_market_data` and publish only after accepting the result:

```cmd
cd /d %MARKET_DATA_ROOT%

python probes\publish_sampling_hierarchy.py ^
  "%V2_JOURNAL_ROOT%\%TARGET_DATE%.sqlite3" ^
  "%OV_R1_DIR%\sampling_hierarchy_r1.json"
```

Expected publication result:

- `Result: inserted`;
- revision `r1`;
- the same 09:30 ET effective time as `r0`;
- unchanged Uni count;
- nonzero Focus count no greater than `FOCUS_LIMIT`;
- Hot count zero.

Validate before starting pollers:

```cmd
python probes\audit_quote_observation_journal.py ^
  "%V2_JOURNAL_ROOT%\%TARGET_DATE%.sqlite3" ^
  --show-missing 20

python probes\replay_quote_observation_journal.py ^
  "%V2_JOURNAL_ROOT%\%TARGET_DATE%.sqlite3" ^
  --progress-every 0
```

Replay should now show two membership events, `r0` then `r1`, and project the
accepted Uni and Focus counts with no acquisitions or observations.

### 5.5 Optional ToS display publication

ToS receives only the accepted Focus roster. It does not supply OV data and is
not read back for verification. Confirm the display-only scanner loop is
healthy, then submit the roster from `focus_symbols.csv`:

```cmd
mb-scan-status
powershell -NoProfile -Command "$s=@((Import-Csv '%OV_R1_DIR%\focus_symbols.csv').symbol); & mb-scan-command replace_wl_symbols --symbols $s --wait 120; exit $LASTEXITCODE"
mb-scan-status
```

`processed` means the display adapter completed its command path; it is not an
observed-membership guarantee. Do not run `export_wl`, do not compare a ToS
CSV, and do not block API polling or Focus publication on ToS display state.

### 5.6 Retrospective OV cutoff analysis

This analysis is not part of opening production and does not change the
accepted 08:25 selector. Run it only at or after 09:30 ET. One API request per
opening-Uni symbol retrieves five-minute extended-hours candles through 09:30
and calculates cumulative volume at 08:25, 09:00, 09:15, 09:25, and
`OV_FINAL` at 09:30.

```cmd
python probes\analyze_api_ov_cutoffs.py ^
  --opening-proposal "%OPENING_R0%" ^
  --production-ov-manifest "%API_OV_MANIFEST%" ^
  --limit %FOCUS_LIMIT%
```

The run is accepted only when all symbols succeed and every recalculated 08:25
value matches the immutable production bundle. Its separate evidence directory
contains:

```text
cutoff_metrics.csv
cutoff_membership.json
cutoff_candles.jsonl
manifest.json
```

`OV_FINAL` includes the completed 09:25--09:30 candle. It is a retrospective
benchmark and a possible input to a later post-open revision; it cannot be used
to produce the hierarchy already effective at 09:30.

## 6. Before the open: start polling

Use separate command windows for independent pollers. Start them before the
first configured slot.

### 6.1 Schema-v2 Uni poller

```cmd
python probes\probe_universe_quote_watch.py ^
  --watchlist-kind uni ^
  --start-at %TARGET_DATE%T09:30:00%ET_UTC_OFFSET% ^
  --stop-at %TARGET_DATE%T16:00:00%ET_UTC_OFFSET% ^
  --journal-root %V2_JOURNAL_ROOT% ^
  --journal-schema-version 2
```

Do not provide `--universe-csv` or `--symbols`. The poller reads the latest
effective Uni membership from the schema-v2 journal before every slot.

Uni slots are `:00` and `:30` each minute. The last regular-session slot is
15:59:30 ET; the 16:00 stop is excluded.

### 6.2 Schema-v2 Focus poller

After `r1` passes audit and replay, start the Focus poller in its own window:

```cmd
python probes\probe_universe_quote_watch.py ^
  --watchlist-kind focus ^
  --start-at %TARGET_DATE%T09:30:00%ET_UTC_OFFSET% ^
  --stop-at %TARGET_DATE%T16:00:00%ET_UTC_OFFSET% ^
  --journal-root %V2_JOURNAL_ROOT% ^
  --journal-schema-version 2
```

Focus slots are `:05`, `:20`, `:35`, and `:50` each minute. Both pollers write
to the same schema-v2 daily journal, but each acquisition is bound to its own
channel and the exact hierarchy revision effective for its scheduled slot.

Do not also run the legacy schema-v1 Focus poller for this session.

### 6.3 During polling

Each sample should show:

- its scheduled channel slot;
- request and symbol-status counts;
- acquisition, journal-write, and total durations;
- quote and trade freshness summaries;
- any invalid, missing, or request-error outcomes.

Use `Ctrl+C` only when an intentional early stop is required. At 16:00 ET the
normal poller reports `Reached the end of the polling window` and a final
summary.

## 7. After the close: validate and replay

### 7.1 Schema-v2 journal

```cmd
python probes\audit_quote_observation_journal.py ^
  "%V2_JOURNAL_ROOT%\%TARGET_DATE%.sqlite3" ^
  --show-missing 20
```

```cmd
python probes\replay_quote_observation_journal.py ^
  "%V2_JOURNAL_ROOT%\%TARGET_DATE%.sqlite3" ^
  --progress-every 0
```

The audit must report `PASS`. Check:

- SQLite integrity is `ok`;
- the schema is `2` and membership is `nested-uni-focus-hot-v1`;
- acquisitions equal expected slots for each active channel;
- skipped plus acquired slots equal expected slots;
- missing slots and row mismatches are zero;
- observation totals and status counts are plausible.

Replay must reproduce the expected membership revisions, acquisition totals,
observation totals, final projected state, and a deterministic sequence hash.

### 7.2 Legacy schema-v1 Focus journal

If the separate legacy Focus process was used:

```cmd
python probes\audit_quote_observation_journal.py ^
  "output\quote_observation_journal\%TARGET_DATE%.sqlite3" ^
  --show-missing 20
```

```cmd
python probes\replay_quote_observation_journal.py ^
  "output\quote_observation_journal\%TARGET_DATE%.sqlite3" ^
  --progress-every 0
```

Schema v1 should be labeled `legacy-independent-channels`. Do not copy or
merge its tables into the schema-v2 daily database.

## 8. Inspect a completed day in the dashboard

Open a completed journal at its final state:

```cmd
python probes\dashboard_quote_observation_journal.py ^
  "%V2_JOURNAL_ROOT%\%TARGET_DATE%.sqlite3"
```

The default address is:

```text
http://127.0.0.1:8050
```

Open paused before the first event instead:

```cmd
python probes\dashboard_quote_observation_journal.py ^
  "%V2_JOURNAL_ROOT%\%TARGET_DATE%.sqlite3" ^
  --start-at-beginning
```

Use the dashboard's **Stop server** control or `Ctrl+C` in the command window
for a graceful shutdown.

## 9. Useful diagnostic commands

These probes answer focused questions; they are not all part of the routine
daily production path.

### One historical daily candle

```cmd
python probes\probe_daily_ohlcv.py --symbol DAIC --date 2026-09-18
```

### Complete current Schwab quote response

```cmd
python probes\probe_quote.py --symbol DAIC
```

### Five-minute extended-hours price history

```cmd
python probes\probe_price_history.py --symbol DAIC --date 2026-09-18
```

### Current Nasdaq trade-halt feed

```cmd
python probes\probe_nasdaq_trade_halts.py
```

### Nasdaq trade halts for one date

```cmd
python probes\probe_nasdaq_trade_halts.py --date 2026-09-18
```

### Continuous LUDP/M monitor

```cmd
python probes\monitor_nasdaq_trade_halts.py
```

The monitor polls every 60 seconds and reports only newly seen LUDP/M symbols.
Press `Ctrl+C` to stop it.

### Short polling smoke test

Use a separate test journal root so the result cannot collide with production:

```cmd
python probes\probe_universe_quote_watch.py ^
  --symbols SPY QQQ AAPL NVDA ^
  --watchlist-kind focus ^
  --start-at %TARGET_DATE%T09:30:00%ET_UTC_OFFSET% ^
  --stop-at %TARGET_DATE%T09:31:00%ET_UTC_OFFSET% ^
  --max-samples 2 ^
  --journal-root output\quote_observation_journal_smoke ^
  --journal-schema-version 1
```

Choose a future window. The exact Focus cadence determines how many slots fall
inside it; `--max-samples` is an upper bound, not a request to invent slots.

## 10. Failure and recovery notes

| Symptom | Meaning and action |
|---|---|
| `output directory already exists` | Evidence is immutable. Preserve it and rerun with a distinct `--output-dir` suffix. |
| Credential preflight fails | Run `mb-schwab-auth`, then restart the poller before the first required slot. |
| `--publish-at must be in the future` | The requested publication time has passed. Do not backdate; choose a genuinely future controlled window. |
| Publication time is not before `effective_at` | The revision cannot be made causally valid. Build a proposal for a future effective time. |
| No effective schema-v2 hierarchy | Publish the session's valid opening `r0` before starting its pollers. |
| API OV source does not cover complete opening Uni | Do not publish a partial Focus decision. Preserve the failed bundle, diagnose API failures, and rerun into a new output directory before 09:30. |
| API OV opening/session/hash mismatch | Use the exact `OPENING_R0` that produced the API bundle. Never rename or edit evidence to bypass the check. |
| API OV bundle is not production eligible | Do not build or publish r1. Inspect per-symbol status, completion time, and whether the run was only a smoke test. |
| OV selection produced no Focus symbols | Preserve the API evidence and decision bundle and investigate before publishing. |
| Audit reports missing slots | Preserve the journal and investigate the poller console, error log, credentials, and process lifetime. Do not edit the database to hide the gap. |
| Audit reports row mismatch or integrity failure | Stop using that journal as accepted evidence until the cause is understood. Preserve all raw files. |
| Dashboard port 8050 is occupied | Stop the existing server or add `--port` with another local port. |

## 11. Current capability boundary

Implemented and operational:

- deterministic post-close Uni selection with a per-symbol decision ledger;
- strict schema-v2 opening `r0` generation and publication;
- complete opening-Uni API-only OV acquisition with immutable candle evidence;
- Uni-constrained API-OV Focus production with durable evidence and a strict
  schema-v2 `r1` proposal;
- independent exact-slot Uni and Focus polling;
- daily SQLite journaling, read-only audit, deterministic replay, and dashboard;
- Schwab quote/history diagnostics and Nasdaq halt acquisition/monitoring.

Not yet part of routine production:

- historical 3/5/10/30-session OV statistics and richer scoring;
- routine live validation of the new API-only opening pipeline and all Hot
  operation;
- intraday admissions and promotions driven by developing facts;
- automatic exchange-calendar date selection;
- distillation of completed daily journals into the long-term historical
  analytics database.

Until historical distillation exists, retain the daily SQLite journals and
their associated raw evidence, CSV outputs, manifests, and logs.

## 12. Maintaining this manual

Update this document in the same pull request whenever an operator-facing
command, default, output path, validation rule, schema boundary, or production
status changes. Each update should:

1. verify the documented syntax against the command's `--help` output;
2. keep the shortest safe production path near the top;
3. mark transitional or future commands explicitly;
4. preserve old runbooks as dated evidence rather than rewriting historical
   results;
5. update the **Last verified** line.

Related detailed documents:

- `docs\Daily_Universe_Production_Runbook.md`
- `docs\Schema_v2_Live_Like_Transition_Runbook.md`
- `docs\Atomic_Hierarchical_Membership_Contract_and_Implementation_Plan.md`
- `docs\Canonical_mb_package_Project_Roadmap.md`
