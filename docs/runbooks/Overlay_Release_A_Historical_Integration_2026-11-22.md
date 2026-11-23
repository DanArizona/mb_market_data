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


## 2026-11-23 qualification observations and planned workspace refinements

**Observed on MasterBot from the isolated Release A checkout** at the original
historical integration commit `cfadea5013eb9b3d0ffdc3db8e74a13e1bf0eacd`:

- `call verify_overlay_workspace.cmd`: **72/72 PASS** with Dash 2.18.2,
  Plotly 6.5.2, no provider calls and no production changes. This qualifies
  the offline suite, not a production deployment.
- The November 13 2026 real journal loaded **11,742 replay events** into the
  localhost Dash viewer; optional daily Nasdaq evidence was attached.
- At about **09:33:13 ET**, Membership showed **515 Uni-only / 40 Focus-only /
  6 Hot**; ONFO had **r5, $0.72 last price, Halted**. Charts showed
  **33 quote outcomes and 0 completed OHLCV candles**.
- At about **10:28:44 ET**, the lists showed **512 Uni-only / 40 Focus-only /
  18 Hot**; ONFO had **r30, $1.00 last price, Normal**. The chart showed
  **1,697 ONFO quote outcomes** and multiple replay-visible Nasdaq markers.
- A **backward seek to 09:33:13** restored the earlier r5 membership,
  price, security state, counts and 33 charted quote outcomes. These are
  desktop visual observations supporting historical causal replay.
- Monitor at the earlier time showed **103 events applied**, **4,794
  observations**, 561 unique members and 46 multi-channel members.
  Its footer explicitly reported **"Acquisition health · not attached"**.
  The real November 13 strict journal audit remains **FAIL** for six missed
  Hot slots; the Monitor does **not** currently display that external audit
  result. Do not claim collection-health/audit visibility has passed.
- The viewer's current Charts subtitle says "Completed five-minute OHLCV"
  even when its counter correctly reports zero candles. Correct that
  conditional wording as a small presentation fix.
- **Graceful shutdown PASS (2026-11-23):** the operator reported that
  clicking **Stop server** in the genuine November 13 Historical Overlay
  returned the viewer's CMD window to its prompt. This establishes the
  viewer's browser-initiated process shutdown.
- **TCP port release PASS (2026-11-23):** in the same isolated `dashing`
  checkout, a separate read-only `Get-NetTCPConnection -LocalPort 8051
  -State Listen` check returned **`PORT_8051_FREE`** after **Stop server**.
  No process was listening on TCP port 8051. A repeated viewer restart and
  active-collector isolation test remain separate later qualification checks.

**User-approved planning notes** (recorded, not implemented here):

1. **Harmonize Historical Membership with the newer accepted Membership UI.**
   Retain the three compact sorted/exclusive tier lists, shared Find input,
   inspected-symbol details, automatic selection/scroll on read-only search,
   and informative empty/unavailable states. Reuse only presentation and
   read-only selection behavior from
   `experiment/overlay-membership-bridge-20261119`. In Historical mode,
   **do not expose or register drag/drop, admission, Preview, Confirm, worker
   polling, or write-capable callbacks**. Label historical membership and
   quote provenance explicitly as of the replay clock. This should precede
   final Release A production interface acceptance.
2. **Add genuine OHLCV candles and volume to Historical Charts.**
   The existing source has a separately acquired immutable one-symbol,
   one-date five-minute Schwab cache and correct interval-completion
   visibility. Qualify actual cache presentation with a **matching completed
   historical date and selected symbol**; use the cached evidence, not
   provider requests in the viewer. Acquisition and authentication, if
   needed to generate a missing cache, are a separate explicitly attended
   activity outside active polling. No cache means **quote-only**, with
   a truthful headline and no invented OHLCV or volume bars.
3. **Add a selected-chart-symbol control** in the Graph Setups sidebar.
   Synchronize it with Membership selection so that clicking a ticker or
   submitting a canonical ticker updates the shared inspected symbol,
   Charts, and inspector consistently. Rebuild the view using the current
   historical session's evidence and any exact symbol/date OHLCV cache.
   Do not silently reuse ONFO's cached candles for another ticker.
   This is a target for the production-readiness UI gate.
4. **Add a historical session date selector**, preferably a calendar popup
   in the Information or Time Controls sidebar. Discover available dates
   from **validated, accessible completed-session journals**, not merely
   filename patterns; indicate available/unavailable dates accurately.
   Switching dates must update journal identity, replay clock, Nasdaq
   evidence, symbol context, OHLCV cache and health/audit source together,
   with no production write or cross-date carryover. This is a **planned
   enhancement** and can follow the Release A step 4A standalone session
   attachment/catalog; no calendar has been implemented yet.

**Sequencing:** Keep the approved Release A order
`1 → 2 → 4A → 3 → 4B → 5`. Step 2 real-data visual checks are substantially
advanced: browser-initiated graceful shutdown is now observed, while
health/audit attachment, genuine same-session OHLCV, repeated restart, and
active-collector isolation evidence remain open. Browser shutdown and TCP
port-release have passed. Steps 4A and 3 should provide the reusable session-discovery and
live-read infrastructure; the interface refinements above can be built in
isolated increments alongside this work and assessed before final Release A
release approval. All editing controls belong to Release B and remain
disabled in Historical mode.
