# mb_market_data Operations Quick Reference

**Purpose:** A concise operator manual for routine `mb_market_data` work.  
**Last verified:** 2026-09-19.
**Shell:** Windows Command Prompt (`cmd.exe`).  
**Working directory:** `C:\Users\danla\Documents\github\mb_market_data`.

This is the maintained starting point for day-to-day commands. Detailed design
documents and runbooks remain authoritative for implementation details, but an
operator should be able to run and validate the normal workflow from this
document.

The current edition covers `mb_market_data`, the daily OV-to-Focus bridge in
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
python -m pytest -q
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
set V2_JOURNAL_ROOT=output\quote_observation_journal_v2
set MARKET_DATA_ROOT=C:\Users\danla\Documents\github\mb_market_data
set WATCHLIST_ROOT=C:\Users\danla\Documents\github\schwab_watchlists
set FOCUS_LIMIT=40
```

Definitions:

| Variable | Meaning |
|---|---|
| `SESSION_DATE` | The just-completed regular trading session used to select Uni. |
| `TARGET_DATE` | The next intended trading session. |
| `ET_UTC_OFFSET` | Eastern offset on `TARGET_DATE`, including daylight-saving time. |
| `V2_JOURNAL_ROOT` | A dedicated schema-v2 journal root. Use a purpose/date suffix for a controlled test. |
| `MARKET_DATA_ROOT` | Local `mb_market_data` repository root. |
| `WATCHLIST_ROOT` | Local `schwab_watchlists` repository root. |
| `FOCUS_LIMIT` | Explicit maximum size of the OV-derived opening Focus set. `40` is an example, not a hidden project default. |

For the controlled September 21 opening test, the journal root was:

```cmd
set V2_JOURNAL_ROOT=output\quote_observation_journal_v2_opening_2026-09-21
```

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
powershell -NoProfile -Command "$u=Import-Csv 'output\daily_universe_production\%TARGET_DATE%-from-%SESSION_DATE%\universe\uni_symbols.csv'; 'UNI COUNT: ' + @($u).Count"
```

Explain a particular symbol from the decision ledger:

```cmd
powershell -NoProfile -Command "Import-Csv 'output\daily_universe_production\%TARGET_DATE%-from-%SESSION_DATE%\universe\decision_ledger.csv' | Where-Object symbol -eq 'DAIC' | Format-List *"
```

Replace `DAIC` with the symbol under investigation. The ledger is the
authoritative answer to why a directory symbol was included or rejected.

### 4.4 Publish opening membership to schema v2

After accepting the roster, publish its already-generated `r0` proposal:

```cmd
python probes\publish_sampling_hierarchy.py ^
  "%V2_JOURNAL_ROOT%\%TARGET_DATE%.sqlite3" ^
  "output\daily_universe_production\%TARGET_DATE%-from-%SESSION_DATE%\opening_hierarchy_r0.json"
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
It preserves `Focus ⊆ Uni`; it does not calculate historical OV on MasterBot.
The current bridge consumes the same-day ToS `OV_DECISION` custom column.

### 5.1 Seed the ToS source Watchlist with opening Uni

After accepting the opening roster, replace the dedicated ToS `Default`
Watchlist with the complete generated Uni. This is a GUI mutation, so first
confirm that the El-Cheapo scanner command loop and ToS are operational:

```cmd
mb-scan-status
mb-scan-command suspend_exports --wait 30
```

From the `mb_market_data` repository root:

```cmd
powershell -NoProfile -Command "$s=@((Import-Csv 'output\daily_universe_production\%TARGET_DATE%-from-%SESSION_DATE%\universe\uni_symbols.csv').symbol); & mb-scan-command replace_wl_symbols --symbols $s --wait 120; exit $LASTEXITCODE"
```

Check `mb-scan-status` until the replacement job has finished, then resume
scheduled exports:

```cmd
mb-scan-status
mb-scan-command resume_exports --wait 30
```

Do not continue if the replacement failed or the observed Watchlist count does
not match the generated Uni count.

### 5.2 Obtain the same-day OV export

Allow ToS to populate `OV_DECISION`, then request an explicitly dated export:

```cmd
mb-scan-command export_wl ^
  --target-filename "%TARGET_DATE%-OV-WL.csv" ^
  --wait 30
mb-scan-status
```

Confirm the GUI job completed and locate the transported file in the configured
`MB_SCANS` directory. Set its full path explicitly; for example:

```cmd
set OV_WATCHLIST=C:\Users\danla\Documents\github\stockScans\2026-09-21-OV-WL.csv
```

The filename date must equal `TARGET_DATE`. The CSV must contain `Symbol` and
`OV_DECISION`, and it must include every opening-Uni symbol. The producer
allows extra rows but labels them `outside_uni` and cannot select them.

### 5.3 Build the Focus `BASE_SET` and `r1` proposal

Run this from the `schwab_watchlists` repository:

```cmd
cd /d %WATCHLIST_ROOT%
git pull --ff-only origin main

python run_ov_focus_production.py ^
  --watchlist "%OV_WATCHLIST%" ^
  --opening-proposal "%MARKET_DATA_ROOT%\output\daily_universe_production\%TARGET_DATE%-from-%SESSION_DATE%\opening_hierarchy_r0.json" ^
  --limit %FOCUS_LIMIT%
```

Enter the encrypted Schwab configuration password when prompted. A successful
run reports `OV Focus production: PASS` and writes an immutable directory under
`output\ov_focus_production`. It contains:

```text
ov_decision_evidence.jsonl
focus_decision_ledger.csv
focus_symbols.csv
sampling_hierarchy_r1.json
manifest.json
```

Copy the printed output directory into a variable. Example:

```cmd
set OV_R1_DIR=C:\Users\danla\Documents\github\schwab_watchlists\output\ov_focus_production\2026-09-21-08-25-30
```

Review the reported source, eligible, and selected counts. The selected count
may be below `FOCUS_LIMIT` only when fewer symbols were eligible. Inspect the
complete decision-reason distribution:

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
| OV source does not cover complete opening Uni | Do not publish a partial Focus decision. Correct the ToS source roster/export and rerun into a new output directory. |
| Watchlist date differs from opening session | Use a genuinely same-day export or supply `--watchlist-date` only for an undated filename. Do not rename stale evidence to bypass the check. |
| OV selection produced no Focus symbols | Preserve the source export and investigate OV/custom-column and quote statuses. Do not publish an empty `r1`. |
| Audit reports missing slots | Preserve the journal and investigate the poller console, error log, credentials, and process lifetime. Do not edit the database to hide the gap. |
| Audit reports row mismatch or integrity failure | Stop using that journal as accepted evidence until the cause is understood. Preserve all raw files. |
| Dashboard port 8050 is occupied | Stop the existing server or add `--port` with another local port. |

## 11. Current capability boundary

Implemented and operational:

- deterministic post-close Uni selection with a per-symbol decision ledger;
- strict schema-v2 opening `r0` generation and publication;
- Uni-constrained, ToS-derived OV Focus production with durable evidence and a
  strict schema-v2 `r1` proposal;
- independent exact-slot Uni and Focus polling;
- daily SQLite journaling, read-only audit, deterministic replay, and dashboard;
- Schwab quote/history diagnostics and Nasdaq halt acquisition/monitoring.

Not yet part of routine production:

- MasterBot-derived historical Overnight Volume analytics and a fully
  automated Focus `BASE_SET` independent of ToS custom expressions;
- routine schema-v2 Focus operation, pending the first full-session production
  validation, and all Hot operation;
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
