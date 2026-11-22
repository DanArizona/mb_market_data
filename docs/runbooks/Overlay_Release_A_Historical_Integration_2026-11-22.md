# Release A — historical Overlay integration and completed-session qualification

**Date:** 2026-11-22
**Branch:** `experiment/overlay-release-a-historical-20261122`
**Status:** isolated historical integration candidate; NOT production-installed or merged.
**Scope:** Release A steps 1 and 2 only. Never change the production polling controller,
its installed checkout, the current-day journal, membership publication, or credentials.

## Integration lineage and exclusions

- Production main baseline: `de1e5544f583c66ebd80355ad2cbaa55ef8860a6`.
- Accepted historical workspace source:
  `experiment/overlay-workspace-layout-20261116`,
  commit `1b3000489789c4540541c5a8eb3a7c920f49e135`.
- The integration copies **19 changed/added paths exactly**, preserving main's
  `daily_runtime.py`, `daily_flow.py`, `daily_membership.py`,
  `daily_live_overlay.py`, production API polling, and `overlay.cmd`.
- Historical mode stays default and **read-only**. Real Time and Backtest
  are **disabled** in this Release A checkpoint. A local viewer cannot
  command a polling worker.
- The accepted synthetic Membership/drag/admission experiment is retained at
  `experiment/overlay-membership-bridge-20261119` and is **not included**.
  Production editor integration belongs to separately approved Release B.
- One-unlock production polling, automatic Nasdaq admission, and source
  authority remain unchanged.

The historical Membership tab displays the causally effective, exclusive Uni,
Focus, Hot lists; its selected-symbol inspector uses saved journal quote data.
The Charts tab uses committed quote points and, only when independently
available, a *validated* immutable same-session OHLCV cache. No candles
are fabricated. Optional daily Nasdaq event markers respect when reports
were observed, not retrospective knowledge.

## Stage 1 verification — isolated Windows checkout (no provider calls)

Use a *separate clone*, not the existing live installed `mb_market_data`
or the dedicated synthetic Membership checkout.

```cmd
git clone --branch experiment/overlay-release-a-historical-20261122 --single-branch https://github.com/DanArizona/mb_market_data "%USERPROFILE%\mb_overlay_release_a_qa_20261122"
```

Open Anaconda Prompt, enter `dashing`, and verify the checkout in independent
CMD steps:

```cmd
conda activate dashing
```
```cmd
cd /d "%USERPROFILE%\mb_overlay_release_a_qa_20261122"
```
```cmd
call verify_overlay_workspace.cmd
```

**Expected from unchanged historical source: 72 tests** with no Schwab API
requests, no password prompts, and no production changes. Report the result,
Dash/Plotly versions, and any exceptions; do not label it passed in this
integration branch until actually verified on MasterBot.

Structural source gates:
- No import/activation of `SyntheticOverlayMembership` or
  `daily_operator_ui` in the historical Dashboard.
- No changes to daily session startup, poller workers, shared membership
  writer, authentication, journals, or installed controller paths.
- Historical tab and charts remain usable if no additional health file or
  OHLCV cache is supplied.

## Stage 2 verification — real *completed* production journal (read-only)

First confirm the following example retained evidence **actually exists on
MasterBot**. The journal and Nasdaq file were used for the accepted November
13 read-only Overlay investigation; **do not recreate or edit them**.

```cmd
if exist "%USERPROFILE%\dailyflow_qa\daily_flow_candidate_20261106\market_data\output\quote_observation_journal_v2_api_opening_2026-11-13\2026-11-13.sqlite3" (echo REAL_NOV13_JOURNAL_PRESENT) else (echo REAL_NOV13_JOURNAL_MISSING)
```

```cmd
if exist "%USERPROFILE%\dailyflow_qa\daily_flow_candidate_20261106\market_data\output\daily_session\2026-11-13\nasdaq\events.json" (echo REAL_NOV13_NASDAQ_PRESENT) else (echo REAL_NOV13_NASDAQ_MISSING)
```

Only if both exist, start a read-only historical viewer **from the Release A
checkout**, on a distinct localhost port:

```cmd
python -u -B probes\dashboard_quote_observation_journal.py "%USERPROFILE%\dailyflow_qa\daily_flow_candidate_20261106\market_data\output\quote_observation_journal_v2_api_opening_2026-11-13\2026-11-13.sqlite3" --overlay-symbol ONFO --nasdaq-daily-events-json "%USERPROFILE%\dailyflow_qa\daily_flow_candidate_20261106\market_data\output\daily_session\2026-11-13\nasdaq\events.json" --host 127.0.0.1 --port 8051
```

Open `http://127.0.0.1:8051/` in Firefox. No production writes, Schwab
connection, or credentials are required. Use **Stop server** to shut down this
viewer; it must not affect any collection process. Run no second writer.

Acceptance observations to document (completed evidence only):
1. Real session identity/date is displayed; final state is not labeled live.
2. Membership revision at the replay clock matches the journal exactly.
   The three highest-tier lists are mutually exclusive while the underlying
   relationship remains Hot ⊆ Focus ⊆ Uni.
3. ONFO quote points appear only after their acquisition completion time.
   Nasdaq LUDP/M markers do not reveal reports before observation time.
4. Seek backward/forward, play, pause, restart, charts and symbol inspector
   remain causal; switching sidebar/tabs does not mutate replay state.
5. Missing Hot slots remain missing and are not rebranded as audit PASS.
   The November 13 journal's strict audit was FAIL for six Hot misses.
6. Read-only historical mode does not offer live Membership editing,
   drag-and-drop, Preview, Confirm, or a second login.
7. Optional no-cache chart displays quote evidence but **no fabricated
   five-minute candles**. A genuine matching validated OHLCV cache can be
   exercised in a separate later visual test without modifying the journal.
8. Viewer stop/restart and browser refresh do not affect collectors or
   leave a stale local port. Preserve stderr and screenshots for anomalies.

Pass this gate only after both the 72-test offline suite and these real-day
causal/visual checks pass. Keep reported test, audit, and GUI results distinct.

## Work after this checkpoint

**Release A step 4A — standalone session attachment** may be developed
immediately after Historical qualification: locate the current/finished
session, validate a pinned plan and journal identity, and start only the
read-only localhost viewer. It does not need a genuine Real Time workspace.

**Release A step 3 — full Real Time workspace adapter**: reuse the existing
read-only quote-only `daily_live_overlay.py` source and integrate committed
quotes, effective revision, Nasdaq reports and health into the full tabbed
workspace. No SDK client, live candles unless genuine separately captured,
and no shared journal writes. Staleness, reconnect, close and session rollover
need explicit states.

**Release A step 4B — routine production lifecycle** follows step 3:
launch/attach/stop with the daily workflow, distinct viewer supervision,
restart behavior, and no polling dependency on viewer health. Do not modify
installed production until approved.

**Release A step 5**: attended live QA, integrated Windows tests, source review,
rollback plan, updated operator runbook, and explicit merge/deployment
authorization. **Release B**: separately qualify production manual editor,
origin/CSRF and session identity checks, initial preview-only testing, and
approved writer actions.

The extra restrictions on outside-Uni manual-admission eligibility (exchange,
OTC, equity subtype and listing/trading status) remain **deferred** and do not
change the existing strict Schwab EQUITY identity gate or this release.
