# Unified Observation Overlay release — work plan (2026-11-24)

**Decision:** Collapse former Release A (historical Overlay) and Release B (controlled Membership operator interface) into **one release target**. This document supersedes the *release split*, not the validated evidence, isolation boundaries, or deferred ideas in the prior runbooks.

**Current source:** `experiment/overlay-unified-release-20261124`, forked from `experiment/overlay-release-a-historical-20261122` at `ab3a9d2`. Production `mb_market_data` main remains `de1e554` and is not modified by this work. The earlier synthetic Membership prototype is retained independently at `experiment/overlay-membership-bridge-20261119`. Do not treat a working prototype as a production-qualified editor.

## Product goal and boundaries

Deliver one usable local Overlay workspace for observing and inspecting saved/live market data and, in its authorized operator context, controlling the existing atomic Uni/Focus/Hot membership hierarchy. The release is not an order-entry engine, a source of signals, or a trading authorization. It should leave room to add custom indicator development, optimization, and supervised execution later without building those features prematurely.

- **Historical mode:** read-only, immutable evidence; never exposes enabled manual Membership actions or writes membership, market journals, caches, or production controller state. Mode clearly says **Historical — read-only**.
- **Real Time mode:** reads committed live evidence without disrupting the polling owner. Manual operator actions may be enabled **only here**, with existing authoritative backend, explicit target/source, preview/confirmation, policy gates and receipts. No direct mutation of journal history and no second membership writer.
- **Backtest mode:** remains disabled until there is a real versioned simulation adapter. No fictitious playback or simulated live orders.
- The existing Hot ⊆ Focus ⊆ Uni invariant, effective-time atomic revision publication, ordinary halted-symbol demotion (automatic re-elevation allowed), and deferred “Suppress Automatic” decision remain unchanged.
- The Overlay does not replace the daily polling controller, invoke Schwab quote polling, or silently reclassify missing slots or strict-audit failures.

## Essential experience (first implementation focus)

1. **Unified Membership layout in Historical.** Reuse the newer compact three-tier Membership design: shared Find, exclusive Uni-only / Focus-only / Hot lists, one selected symbol, inspector/details. Keep all operator affordances disabled or absent and display explicit historical read-only status. Preserve replay-effective membership, not today's membership.
2. **Candlesticks and volume.** Use the existing Schwab 5-minute immutable OHLCV acquisition/cache path. Accept cache only when symbol, ET session date, frequency, and evidence hash validate. Show a distinct no-cache / invalid-cache state; never draw invented bars or future candles. Preserve quote evidence and halt markers.
3. **One inspected symbol across tabs.** Add a symbol picker in Graph Setups and wire it to the single selected Membership symbol and Charts. Changes must rebuild the proper selected-symbol replay state, reset references that belong to a prior symbol, and recheck cache identity. Do not simply relabel another symbol's candle series. Outside-membership symbols require an explicit empty/unsupported state rather than fabricated evidence.
4. **Historical date calendar.** Show only *discovered and validated* recorded session dates; indicate session availability/status, and attach the chosen session's journal, effective-time revisions, optional Nasdaq events, health and matching OHLCV evidence. Never infer the date from the browser clock or load a neighboring session on failure. Date picker UI follows the read-only discovery layer; it need not block progress on first three items.
5. **Read-only live attachment.** Discover a dated daily controller session and its already-running workers safely. Attach on a separate localhost port, verify new committed data appears, and verify Overlay close/restart never affects Uni/Focus/Hot, membership, Nasdaq monitoring, or next-session preparation.
6. **Controlled operator Membership actions.** Adapt the accepted synthetic browser/control work to the authoritative daily membership backend. Operators can select target symbols and request controlled promotion/demotion in Real Time only; preview and explicit confirmation precede publication. Reject stale previews, expired owner/authentication and invalid hierarchy/authority. Preserve reason/receipt. Ordinary demotion remains the chosen halted-symbol policy. No suppress-automatic action in this release.

## Delivery order

**Tuesday Nov 24 (priority):** Start and verify production polling on MasterBot via established one-unlock daily procedure; do not assume the Nov 23 JSON issue has been fixed. Then run historical Overlay from a separate `dashing` checkout, using a genuine retained journal. Complete safe session discovery/attachment checks and as much of the three essential UI refinements as the screenshot and live evidence permit. If polling fails, use the lightweight incident recovery procedure; do not sacrifice the full day unnecessarily.

**Wednesday Nov 25:** Finish the three essential UI refinements, add calendar presentation if discovery is ready, and qualify read-only live attachment and the controlled membership boundary separately.

**Thursday Nov 26 (Thanksgiving):** Do not count on operator time.

**By Nov 29 (subscription change):** Prefer one usable, documented unified Overlay candidate and a short prioritized plan for the **first custom indicator mechanism and UI** over additional infrastructure refinements. Any unfinished features remain accurately labeled.

The timetable is an ordering of work, not a promise that an unqualified feature will be deployed. No production merge or installation without explicit approval.

## Already validated and retained

- Historical consolidation: 72/72 Windows checks.
- Real Nov 13 replay: 11,742 events, membership and quote causality, Nasdaq first-observed timing, backward seek, stop/port cleanup.
- ONFO: 84 genuine Schwab 5-minute candles independently cached; full chart/volume capability exists with matching cache.
- The newer compact Membership prototype has been visually exercised separately. Import deliberately; do not alter historical replay by adopting synthetic Membership writes.
- Nov 23 quote-response diagnostics passed 308/308 offline checks but were **not** a demonstrated live provider correction.

## Acceptance checklist for this single release

- Both Historical and Real Time show truthful source, date, mode, freshness, and controller status; Backtest is disabled.
- Membership Find, tier lists, inspector, and Charts/Graph Setups selection are synchronized without stale symbol/cache data.
- Historical calendar lists only actual validated journals and reports missing evidence explicitly. Historical has no enabled Membership mutation path.
- Genuine correct-session five-minute OHLCV displays price and volume; absent/wrong cache never becomes bars.
- Viewer can attach/stop/restart without polling interference. All anomalies, gaps, and audit failures remain visible.
- Operator requests are reviewed, confirmed, idempotently recorded through the one authoritative membership path, and blocked when stale or unauthorized.
- Tests, at least one real historical journal exercise, at least one bounded live integration exercise, and an explicit separate production-deployment decision are completed.
- No order placement or trade execution is enabled by this release.

## November 24 — experimental operator-panel progress (NOT production authorized)

An existing operator backend was copied **without modification** from
`experiment/overlay-membership-bridge-20261119` into this unified experimental
branch (`daily_operator_ui.py` plus its static assets). The already established
`OperatorState` and `daily_membership.submit` remain the only Preview / request
submission path. No new membership writer or direct SQLite update was added.

The unified Dashboard now has an **explicit opt-in** launch flag
`--enable-manual-membership-actions`, available only with a verified
`--real-time-data-root` for the current ET session. Without that flag,
**ordinary Real Time and all Historical sessions remain read-only and have no
manual action controls**. With it, the Real Time Membership inspector offers
an editable symbol (including outside-Uni candidates), Promote or Ordinary
Demote, target tier, **Preview**, separate **Confirm and queue request**, and
the existing worker receipt status. Every input change invalidates the current
preview. The operator's own short-lived proposal, owner/worker heartbeat,
revision, action, cap and identity checks are re-evaluated on Confirm.
The web process never publishes a hierarchy revision itself.

The per-viewer operator session token is checked by both action callbacks.
The displayed request status distinguishes **queued** from **effective** and
does not claim success until the existing worker receipt says so. The new
unified panel does not yet implement the synthetic prototype's drag/drop
gesture or automatic selection after a newly effective promotion.

**No production request has been submitted.** Even though the flag exists in
experimental source, do not launch it against production or click Confirm
without a separate attended authorization and a qualified Windows exercise.
The right next check is the **default-disabled / no-network** UI verification,
followed by a bounded synthetic operator exercise.

## Disposable unified Membership GUI qualification

`preview_unified_operator_synthetic.cmd` is an explicit, offline-only
test entrypoint in this experimental branch. It creates a temporary schema-v2
journal with **fabricated** stock prices, a temporary owner heartbeat, and the
**existing daily Membership worker** using a synthetic-only identity fixture.
It opens the actual unified Real Time Membership panel with an unmistakable
OFFLINE SYNTHETIC banner, then deletes temporary files after the in-app
**Stop server** button is used. The normal production read-only viewer and
production launch flags are not needed.

From the separate MasterBot `dashing` experiment checkout:

```cmd
call preview_unified_operator_synthetic.cmd --port 8062
```

Open `http://127.0.0.1:8062` in Firefox. In **Membership**, set INTC,
Ordinary demote, destination Focus. Click **Preview action** and inspect
the before/after and revision. Only then click **Confirm and queue request**.
Look for the worker receipt and the effective Focus membership after the short
lead time. Once effective, preview and confirm promotion back to Hot. Use
**Stop server** in Firefox to close and remove the disposable journal.

The synthetic driver calls `OperatorState` directly against the freshly
seeded temporary plan. The production-only `RealTimeMembershipOperator`
correctly rejects synthetic plans, and the synthetic launcher does not use the
real production plan, Schwab, real credentials, or a production quote journal.
A synthetic identity allowlist is only a fixture; it is **not** a real exchange
listing or security validation.

**This has not yet been exercised in a Windows GUI**, and production control
remains unauthorized. The next gate is the full isolated
`verify_overlay_workspace.cmd` followed by this bounded demo, not a live
operator launch. Drag/drop, auto-scroll following effective changes, OHLCV
automatic acquisition and UI calendar remain pending unified-release features.

## November 24 — drag-to-preview and post-effectiveness following

The isolated synthetic unified Real Time Membership panel has already been
visually verified through `INTC` Hot → Focus (r3) → Hot (r4), with
`EFFECTIVE_AS_PUBLISHED` receipts, no production modifications.

The next experimental checkpoint adds drag-and-drop using the earlier
`membership_drag_bridge.js` AG Grid external-drop-zone implementation.
**Drag emits only a proposal** with the symbol, source tier and destination
tier. The server revalidates the symbol's *actual current effective tier*
against the authoritative operator snapshot and calls the existing 90-second
`OperatorState.preview`. Drag cannot directly queue a request, reorder
recorded row data, or publish a revision. The operator still must click the
separate Confirm button after reviewing action, target and revision.

In ordinary Historical and read-only Real Time, drag controls and the
corresponding callback are absent. In explicitly enabled controlled Real
Time, they are available only within the locally attached operator session.
The manual symbol/action/destination form remains available when AG Grid
drag binding is unavailable. Manual changes now prepare a preview
automatically, with only an explicit Confirm able to queue a request.

A second callback watches the existing worker receipt and the projected
membership roster. It auto-selects/scrolls the symbol to the destination tier
**only once**, and only when the receipt reports
`EFFECTIVE_AS_PUBLISHED` **and** the effective destination row is actually
present in committed journal evidence. A queued or stale preview cannot
cause an apparent completed move; a later superseding revision that has
moved the symbol elsewhere is not followed into the wrong tier.

The existing disposable runner is retained:

```cmd
call preview_unified_operator_synthetic.cmd --port 8062
```

On the Membership tab, drag INTC from Hot into Focus by its grip. Review the
drop-created PREVIEW before confirming. Observe the worker receipt, the
effective destination and automatic selection/scroll. Then drag INTC back
from Focus to Hot, confirm and observe. Stop the local demo using
**Stop server**. Everything remains isolated in a temporary synthetic
journal/worker.

**Status:** experimental source and isolated tests only, pending Windows
verification and a bounded synthetic visual exercise. No production
Membership change is authorized, and genuine Schwab OHLCV Real Time
integration remains a separate upcoming milestone.

## November 24 — preserve effective worker receipt after auto-selection

The controlled synthetic drag confirmed INTC Hot → Focus r3, with the symbol
automatically selected/highlighted in Focus. The user screenshot showed
`No worker receipt` afterward despite the effective revision. Source review
found the UI cause: the automatic symbol echo called the operator selection
invalidation callback, which also set `operator-command` to `None`, thus
discarding the ID used for polling the last worker receipt. This did **not**
undo the actual published Membership revision.

The experimental correction now separates the *editable, invalidatable
preview* from the *last confirmed command and worker receipt*. Invalidations,
new previews and denied previews no longer clear the last confirmed command,
so the receipt remains visible. A newly confirmed action replaces it with
the new command ID. The operator panel labels the queue field
`LAST REQUEST QUEUED` to clarify that the receipt belongs to the previous
confirmed request, not the current editable proposal. A separate callback
regression test covers confirm → automatic selection → preview/refusal with
`EFFECTIVE_AS_PUBLISHED` preserved.

The correction was **visually verified on MasterBot**: the user screenshot
shows INTC effective in Focus at r3, selected automatically, with the worker
receipt still displaying `EFFECTIVE_AS_PUBLISHED`. Historical/read-only
Real Time continue to exclude operator actions; no production files,
credentials, membership worker or main branch are changed.

## November 24 — one-button controlled Membership preview (experimental)

The operator confirmed that two separately labeled buttons were confusing
during drag tests. The user also encountered a disabled Confirm control when
a drag's Hot → Focus demotion intent was temporarily reflected as
`Promote → Focus`. That was a UI event-ordering issue, not authorization
to bypass the backend checks.

**Intended interaction:** an explicit drag or a manual selection of Symbol,
Action and Destination prepares a reviewed, nonmutating **preview**.
The UI exposes only one action button:
`Confirm and queue request`. The server still performs the established
`OperatorState.preview` checks (current tier, hierarchy revision, Hot
capacity, session/worker eligibility, 90-second expiry), and Confirm
revalidates again before queueing. Neither preview nor drag writes directly
to the production journal.

The bridge now emits symbol, action, target and a validated drag-intent store
in one Dash callback. A mismatched intermediate field echo cannot enable
Confirm. A subsequent matching preview or manual field edit prepares the
proposal automatically; an action/destination change invalidates the old
proposal and recomputes it. Backend preview errors leave Confirm disabled.
The last confirmed command ID and worker receipt are retained independently
when preparing other actions.

**Safe startup:** Action and Destination begin unselected; an initial
application load never arms a request. The user deliberately chooses these
fields, or performs a drag, to prepare a preview. After an effective worker
revision, automatic symbol following does not silently change the target
to the reverse action or arm Confirm. The explicit next action is always
operator initiated. An expired proposal requires a new field selection
or another drag.

**Validation status:** experimental branch has additional isolated callback
regression coverage for (a) manual auto-preview and one Confirm, (b) atomic
drag reflection, (c) mismatched intermediate values remaining disabled, and
(d) auto-follow not choosing an automatic reverse action. These changes
still require Windows QA and a fresh synthetic visual check. Historical
and ordinary read-only Real Time remain read-only; main and production
remain untouched.

## November 24 — repeatable one-button Membership visual QA, remaining drag miss

MasterBot's unified synthetic session verified the streamlined workflow:
one dropped Hot → Focus move initially did **not** create a visible
preview. Repeating the drop produced the correct automatic Hot → Focus
preview, and a single Confirm produced effective revision **r3**, an
`EFFECTIVE_AS_PUBLISHED` receipt, and automatic highlight in Focus.
A subsequent Focus → Hot drag preview and one Confirm produced
effective revision **r4**, returned the symbol to Hot and retained the
worker receipt. Main and production remained untouched.

A separate issue was visible after the r3 demotion: the old controls
could offer a new `Focus → Focus` demotion. This is a zero-tier-change
operation, not a meaningful demotion. The experimental operator now
rejects all same-tier demotions (target must be strictly lower than
the current effective tier).

A bounded drag-reliability correction adds a `dragStopped` fallback
for an AG Grid row released over another visible tier panel, using
the current pointer location to identify the intended target. It
deduplicates the native external-drop-zone callback and the fallback
for the same gesture. A missed, invalid or unrecognized drop now shows
an explanatory status in Membership instead of leaving the operator
guessing. Both paths still create only advisory preview requests;
neither changes the browser's roster or queues a worker command.
External-drop-zone support is documented by Dash AG Grid and
the upstream AG Grid dragStopped/row drag event contracts.

**Status:** script syntax checked and new offline regression checks
added; Windows 11 Firefox visual drag reliability remains unverified.
The exact cause of the first missed drag has not been established.
The full suite and one fresh synthetic drag need MasterBot verification.
After that bounded test, prioritize genuine Schwab 5-minute OHLCV
integration rather than extending the Membership prototype further.

## Deferred Membership list refinements — accepted user direction (November 24)

Do **not** implement these during the immediate real-time OHLCV integration work;
retain as agreed future UI refinements. Earlier drag-and-drop prototype decisions
take precedence over the current unified Membership grid presentation.

- **Tier-row columns:** remove the per-list `Name` column. The selected
  security's name and description belong in the existing **Symbol details**
  inspector, avoiding repetitive wide columns in Uni / Focus / Hot.
  The resulting compact list should show **Symbol + one metric column**.
- **Tier list search/filter:** remove the per-grid search/floating-filter
  fields. The unified **Find symbol** field remains available; do not
  eliminate that shared lookup.
- **Sorting:** put the ascending/descending selector in each tier's
  header (as established in the earlier drag-and-drop demo), rather than
  a filter/search line inside the grid. Confirm desired initial order
  and whether sort applies by Symbol or the chosen metric at implementation.
- **Later metric selector:** make the current `Last` column a configurable
  display via a dropdown with `Last`, `%change`, and `%range`. Requested
  calculations, preserving the user's definitions:
  `%change = 100 * (last / open - 1)`;
  `%range = 100 * (max - min) / open`.
  Determine the precise source and time window for `open`, `max`, and
  `min`, treatment of missing/zero open, **when we work on the feature**. The user clarified that `%range`
  explicitly includes the factor of `100`; that part is settled.

This is an accepted deferred UX direction, not a completed change or a
reason to delay the next priority: **genuine Schwab five-minute OHLCV
candles and volume in the unified Real Time Overlay**. No production
source, credentials, workers, or installed files are to be changed.

## Wednesday November 25 — intraday completed Schwab OHLCV path (experimental)

**Development objective:** add genuine five-minute OHLCV candles/volume for
the selected Real Time Overlay symbol, without a second provider client in
the viewer and without changing the daily quote-polling controller.

This experimental checkpoint introduces a **read-only evidence-fed first
phase**, not yet actual automatic Schwab acquisition in the running
November 25 production session:

1. `overlay_live_ohlcv.acquire_live_ohlcv(client, ...)` accepts only a
   separately authorized, already-authenticated Schwab SDK client supplied
   by its caller. It requests one selected symbol with
   `price_history(frequencyType="minute", frequency=5)` and an exact ET
   request cutoff. It filters out Schwab's potentially **provisional
   current candle**. A bar is eligible only if its whole five-minute
   interval ended by the *request* time, not just by response receipt.
   No credentials, user input, network client creation or polling loop
   reside in this library.
2. `publish_live_ohlcv(root, acquisition)` publishes one independently
   stored **immutable** bundle: original raw response bytes and a validated
   normalized cache. It writes into a staging directory and exposes the
   complete bundle with one atomic rename. The live viewer is NOT a
   publisher and does not write the production quote journal.
3. `LiveOHLCVReader(root, session_date).latest(symbol)` discovers the
   newest bundle for one exact symbol and ET date. It verifies raw
   SHA-256, bundle timestamp/name, cached candle data versus the source,
   completion boundaries and provenance. A malformed latest bundle is
   rejected instead of silently relabeled as valid prior data.
4. An opt-in `--real-time-ohlcv-root PATH` on
   `probes/dashboard_quote_observation_journal.py` reads these
   independent bundles **only** alongside a validated
   `--real-time-data-root` and an explicit `--overlay-symbol`. It is
   incompatible with the fixed historical OHLCV-cache option. On each
   existing 5-second Real Time tick, the selected symbol's newest
   validated bundle is applied; symbol changes recheck cache identity.
   Without a bundle the viewer stays truthful **quote-only**.
5. `ObservationOverlayProjector.update_live_cache()` rejects future
   candles, cache rollbacks, changed completed bars, wrong symbol/date,
   and conflicting acquisitions. The Real Time controller attaches
   this evidence without adding fabricated quote events or changing
   journal replay counts/timestamps. Historical mode keeps its original
   immutable cache and rejects live updates.
6. The historical Plotly renderer is reused unchanged, including
   rising-price dark green `#1A6B57`, falling-price dark red
   `#803D43`, and **uniform light-blue volume `#6fc8ff`**. Live
   display does not recolor volume green/red.

**Current qualification:** offline tests for cutoff, provisional response
bars, immutable source/cache integrity, selection isolation, monotonic
updates, and retained blue volume. The experimental suite expects
**141** tests (129 previously qualified plus 12 new); MasterBot results
and GUI review are still pending.

**Important remaining dependency:** the running November 25 controller
does not publish these new intraday bundles, and this checkpoint deliberately
does not create a separate Schwab client, credential prompt, or uncontrolled
poller. The next integration decision is how a *single authorized SDK
client owner* should schedule post-five-minute-boundary price-history
requests and publish these bundles for the selected symbol, without
affecting Uni/Focus/Hot quote cadence. That production integration and
real Schwab response qualification require explicit separate approval.
Do not claim real-time candle acquisition has been verified until it has.

## November 25 — owned five-minute OHLCV scheduler prototype (not deployed)

After MasterBot verified **141/141 offline tests PASS**, repository inspection
confirmed that the production `daily_runtime` one-unlock controller launches
distinct authorized Uni/Focus/Hot worker processes, with client initialization
serialized by `auth_lock(ecfg)`. The controller authenticates once attended
and passes the credential to its own supervised children in memory/pipe.
It is **not** a literal shared Python SDK client instance. Any OHLCV worker
must be an explicitly owned, authorized controller role, not a second
uncontrolled operator-launched poller or an API-calling browser.

The experimental `overlay_ohlcv_schedule.OwnedOHLCVSchedule` is an
**injected-client, no-credential, no-background-thread** orchestration
contract for a future controller-owned OHLCV role:

- A current selected symbol is supplied by the owning process; neither
  this module nor the existing Overlay viewer obtains credentials.
- At most one request **per selected symbol per completed five-minute
  slot**, beginning five seconds after the boundary. Requests are capped
  at **two distinct selected symbols per five-minute slot**, which limits
  user switching from creating an unlimited rate spike.
- Each requested symbol/slot is recorded in a **nonsecret durable
  reservation ledger before the provider call**, so explicit owned-worker
  restarts do not immediately reissue the same request. Errors do not
  trigger tight-loop retries; the next five-minute slot is the next
  opportunity.
- The opening and closing ET times are supplied by the authoritative
  session plan; early-close sessions are supported. The final completed
  interval can be captured in a bounded two-minute post-close window,
  with the Schwab query and cache capped to the actual exchange close
  (never silently to 16:00 on an early-close day).
- Publishing uses the separate immutable raw/cache evidence bundle
  validated earlier; the quote journal and membership records are never
  mutated by this component.
- Eight additional offline tests cover pre-boundary suppression, live
  completed-bar acquisition at the boundary, restart idempotence,
  owner availability, two-symbol quota, failed-request backoff,
  early closing and malformed schedules.

**Checkpoint scope:** experimental scheduling and acquisition-library code
only. The suite now expects **149 offline tests** (141 previously qualified
plus eight new scheduler tests). As of this revision they have **not yet
been run on MasterBot**. This does not install an OHLCV worker, modify a
running controller, request Schwab data, or activate any Real Time API
session. The next implementation step is to add an explicitly supervised
OHLCV child to a **future experimental daily-controller candidate**, with
authenticated client creation through the established controller auth lock,
an exact selected-symbol handoff, status/health reporting and a stop/exit
lifecycle. No production deployment without a separate decision.

## November 25 — controller-owned OHLCV integration checkpoint (experiment only)

The existing 149/149 tests passed on MasterBot. The next experimental
source checkpoint now includes a **fully connected, explicitly opt-in
controller-owned OHLCV role**: `--overlay-ohlcv` for a future daily
controller plan, authenticated child with standard private credential pipe,
provider backoff and owner heartbeat, bounded five-minute request scheduling,
nonsecret symbol intent from the Real Time Overlay via
`--enable-ohlcv-selection` on loopback, immutable candle publication and
read-only chart consumption. Normal runs with no opt-in are unchanged.

The worker/selection handoff has an additional eight offline tests.
**157 tests are expected but have not yet been run on MasterBot.**
No current November 25 production processes were replaced, stopped or
modified; `main` is unchanged. This is not yet a verified actual live
Schwab OHLCV session. Use the dedicated
`docs/runbooks/Overlay_Owned_Live_OHLCV_2026-11-25.md` for the integration
contract and remaining qualification criteria. A future experimental
session-specific authorization is required before genuine live acquisition.

## November 25 — explicit Stop server confirmation (experimental)

The operator reported an apparent Stop server failure after the Nov 25
Real Time Overlay session: Firefox still showed the chart with a
`Stopping…` label. The saved **daily polling controller** independently
reported `FINISHED` with Uni/Focus/Hot worker exit 0 and both Membership
and Nasdaq workers stopped. The local Overlay viewer is **not** the
daily-session controller; the Stop server button must never stop or
modify production quote collection.

The existing `LocalDashboardServer` retained its September graceful
stop-event and main-thread shutdown implementation. However, the Dash
callback formerly returned `Stopping…` without any browser-side
confirmation. A stopped local WSGI server leaves the old Firefox DOM
visible, which can look like an active application.

The experimental viewer now packages
`assets/overlay_stop_confirmation.js` which, after the confirmed Stop
server click, makes local same-origin HEAD checks. When the listener
is unreachable twice, it covers the stale chart with an explicit
**Overlay viewer stopped — close this browser tab** notice and explains
that daily polling is independent. If the listener still responds after
a short timeout, it says **shutdown not confirmed** rather than falsely
claiming success.

The viewer entrypoint logs
`VIEWER STOP REQUESTED` to its own CMD, followed by the existing
`Dashboard stopped.` after the WSGI thread has exited.
`tests/test_quote_dashboard.py` now checks that this JS is included.
A dedicated loopback-only test in
`tests/test_local_dashboard_server.py` exercises a real Dash
`Stop server` HTTP callback, joins the WSGI server and checks that
the port is no longer accepting connections. It is intentionally
outside the network-denying standard Overlay suite.

**Qualification pending:** actual CMD return/port-closure and Firefox
post-stop confirmation on MasterBot. A stale screenshot alone cannot
prove whether the server is still listening. Do not modify the
production controller, and do not kill a PID without verifying it is
the intended local viewer.

## Next project milestone after a usable Overlay

Propose **one end-to-end custom indicator prototype** on retained historical data: define inputs, calculation contract, visible chart/inspector output, reproducible out-of-sample evaluation and no-lookahead constraints. Indicator optimization and supervised execution will be subsequent explicit milestones. Avoid infrastructure expansion without a demonstrated dependency.

## Operator attention required next

- Start Nov 24 production session via the accepted one-unlock procedure and report controller status plus successful real quote acquisitions.
- Supply the promised Overlay screenshot/feedback; visual-specific UI changes should match the actual layout, not an inferred redesign.
- Keep production code, installed controller, credentials and live journals unchanged while experiment changes are reviewed.

## November 27 — one-user development and combined integration

The operator clarified that the process called "production" is the working
development deployment for one user, and will always have one user. Its intended
useful outcome includes custom indicators and supervised trading. Market data
and recorded evidence are still real; the clarification changes the delivery
process, not the meaning of those records.

**Revised working approach:** prepare one integrated candidate and use one
planned, attended daily session for the genuine-data demonstration, automatic
OHLCV exercise, coexistence checks, and first actual use of that candidate.
The owned worker's first successful response provides the original raw/cache
evidence and chart demonstration; later intervals in the same session provide
the repeated-update evidence. A separate successful one-shot run is no longer
a prerequisite. Keep the one-shot probe available for diagnosis when useful.

### Minimum preparation and combined session

1. Correct the existing OHLCV restart-command routing and worker/controller
   status identity. Preserve the last successful acquisition and actual latest
   completed candle time separately from the current scheduler tick, so an
   idle tick does not erase useful acquisition evidence. Use existing status
   output, without adding another alarm subsystem.
2. Verify the changed paths and run the existing fast workspace suite once on
   the changed candidate. The accepted MasterBot `dfb1fd6` result remains
   **165 tests PASS, zero failures/errors/skips** for that earlier source.
   Do not repeat unchanged qualifications merely to create another stage.
3. Use the candidate as the single daily owner for its planned session, with
   OHLCV selected in the original plan. Observe first genuine candles, several
   subsequent five-minute intervals, one symbol change, useful status and the
   existing restart path, viewer independence, and the final interval/cleanup
   during the same ordinary use. Preserve quote coverage and audit findings.
4. Record the exact source, launch configuration, evidence locations and any
   unresolved findings. A successful combined session is also the first actual
   use of that version; an extra uneventful "first production day", release
   rehearsal, or fixed number of clean days is not a prerequisite. Repeat only
   the affected check when a concrete finding or later change requires it.

Fresh daily startup still precedes the existing 09:29 ET selection deadline.
The combined exercise does not add a competing owner or rewrite a running
session's plan. Existing authorization for experimental development remains;
activation of the concrete daily candidate and changes to installed/main code
follow the operator's existing authorization boundaries.

### Advance the intended useful product alongside OHLCV

Build the first small indicator calculation/display path against retained
historical candle data while live OHLCV observations are unavailable. Reuse
that input/output path with live validated candle snapshots. Indicator work
does not wait for every possible OHLCV condition to be observed, nor for an
arbitrary clean-day count. No particular first formula or trading strategy is
selected by this delivery decision. Optimization and supervised execution stay
subsequent explicit capabilities; this change does not enable broker orders.

Provider lateness and revisions remain data findings to expose honestly. The
current single-attempt-per-slot schedule and strict completed-candle prefix
policy should be assessed using actual evidence, rather than preemptively
building speculative retry/reconciliation infrastructure. Unrelated GUI
refinements and another large simulation remain deferred.

### Candidate correction and verification recorded November 27

The restart command now uses the existing guarded control-request route. The
worker publishes its child PID for controller status acceptance, and retains
separate last-success/last-attempt records through idle and failed ticks.
Daily status displays the real candle end and acquisition time, with each
record's own symbol. These records describe the current worker lifetime;
after restart, existing saved evidence and the reservation ledger are retained
while the new worker truthfully reports that it has no new successful capture.

Four focused regressions exercise command routing and owner/stop checks,
worker-to-controller status acceptance, retained acquisition/error evidence,
and changed/absent selections. For published `f4af8b3`, the workspace suite
expected **169** tests. It passed **169/169, zero failures/errors/skips**, in the Linux
development environment with Dash 2.18.2 and Plotly 6.5.2. No real provider
requests or desktop visual qualification were performed. This is evidence
for the corrected candidate, separate from the accepted 165-test MasterBot
checkpoint. At publication, the actual combined daily session had not yet
been exercised.

Subsequent MasterBot evidence on November 27 extends that checkpoint:

- The Windows 169-test candidate reported **169/169 PASS in 4.998 seconds**,
  with zero failures/errors/skips, using `dashing`, Dash 2.18.2 and Plotly 6.5.2.
  Its exact MasterBot HEAD was not independently confirmed from pull output.
- On MasterBot, the daily controller used the existing **`closeout-qa`** environment.
  Its import check passed with `exchange-calendars` 4.13.2,
  `mb_tools.schwab_secure` and `tkinter`. The earlier `sea-green` attempt
  stopped before authentication, with plan preparation only. Keep the
  viewer in **`dashing`**.
- The November 27 live controller, PID **50256**, reported authentication
  **VERIFIED**. As of **09:39:28 ET**, all three quote channels reported
  **zero missing slots**; selection, Hot, Nasdaq and membership services
  were running.
- Worker/viewer output reported the first MSFT OHLCV acquisition at
  **14:35:05.825806Z**, with latest completed candle end **09:35 ET**.
  A subsequent update was received at **14:40:06.485353Z**, with **32**
  loaded candles. The cache increased from **31 to 32**; both counts include
  extended-hours candles. The **09:40:24 ET** viewer screenshot
  showed two regular-session candles, volume and fresh chart state.
- The viewer showed a static empty Charts panel while the journal advanced.
  Source review identified a startup path that omits the chart and its callbacks
  when no event is yet effective; the actual viewer launch time was not captured.
  A **viewer-only restart** restored the chart while the healthy daily controller
  continued running.

These status and screenshot observations establish a **partial live
checkpoint**, including two consecutive automatic OHLCV samples. Symbol
handoff and session closeout remain pending; the whole session is not yet
qualified. No independent reconciliation of raw OHLCV numeric values is
claimed by this checkpoint.

The separate local preopen viewer correction retains the already validated
`seek(now)` cutoff when no event has yet been projected, allowing the empty
Overlay and its callbacks to exist from startup. Actual event timestamps
remain empty until committed events arrive. One focused regression covers
both an empty journal and a future-effective initial revision, then renders
a subsequently committed quote in the same Dash application without a restart.
The updated offline suite passed **170/170 in 3.849 seconds**, with zero
failures/errors/skips, in the isolated Linux candidate using Dash 2.18.2 and
Plotly 6.5.2. Network/provider access and credential prompts were blocked.
This correction has **not been deployed**, and Windows verification of the
170-test candidate has **not been run**.

### Single visible candle rendering correction, November 27

The user's three-screenshot sequence showed the first visible regular-session
candle as a wick without a visible body or volume bar; once a second regular
candle appeared, both candle bodies and volume became visible. Source review
and actual Plotly SVG rendering reproduced the count-dependent display defect.

Plotly.py 6.5.2 bundles Plotly.js 3.3.1. When regular-hours filtering leaves
only one timestamp, its automatic spacing calculation falls back to **1 ms**.
The resulting native candle body is **0.49 ms** wide and volume **0.8 ms** wide,
collapsing to zero-width SVG geometry in the tested viewport. Two five-minute
candles supply a **300,000 ms** spacing, producing the expected **147,000 ms**
body and **240,000 ms** volume widths.

The correction preserves the native candlestick's OHLC values, wick and legend.
For one visible candle, a floating Bar draws that same real candle's body at
its actual interval midpoint, with the explicit **147,000 ms** width. It shares
the native candle's name, customdata and legend group, retaining OHLCV hover
and reference selection. The volume trace uses **240,000 ms** explicitly.
The supplemental body disappears when a second candle is visible. A doji
keeps its true zero body height and gains the correctly sized horizontal
outline. The navigator uses native low/high prices and excludes the body's
relative height from its price-range calculation. Acquisition, scheduling,
retained raw responses and cache contents are unchanged.

The installed Plotly.js renderer was exercised under **jsdom**, using figures
generated directly from the modified Python source. Rising, falling and doji
cases produced nonzero main-chart and navigator body widths, visible volume,
and the correct rendered OHLCV hover values. All four candle-field reference
selections and the one-to-two-candle transition were checked. This is actual
SVG rendering evidence, **not Windows desktop acceptance**; no browser or
jsdom dependency was added to the project or its standard verification suite.

One focused regression covers the single regular candle with a hidden
premarket candle, rising/falling/doji bodies, hover/reference fidelity,
navigator bounds and the second-candle transition. The full offline suite
passed **171/171 in 3.967 seconds**, with zero failures/errors/skips, using
Dash 2.18.2 and Plotly 6.5.2. Network/provider access and credential prompts
were blocked. The correction has not yet been applied to the live Windows
viewer; the ongoing session's raw OHLCV numeric qualification is unaffected
by this rendering check.

### November 27 — live display evidence and selected-symbol request policy

After publication of `2f4e71f`, the operator's saved first-MSFT-snapshot check
showed one complete candle body, wick and volume bar on MasterBot. Subsequent
live selection handoffs showed NVDA candles/volume (receipt 10:26:13 ET) and
ZYN candles/volume (receipt 10:30:06 ET). ZYN was reported by the operator as
a Nasdaq-admitted Hot member. Its earlier screenshot showed zero cached
candles; the later screenshot showed 16. The old two-symbol slot allowance
was a plausible explanation for the wait, but the request ledger was not
provided and the exact click-to-display latency was not established.

The operator accepted replacing the prototype's two-symbol-per-five-minute
slot cap with **five OHLCV acquisition attempts per rolling minute**, and
deferring improved collection metrics. This supersedes the November 25
two-symbol quota; it is an application policy, not a claimed Schwab quota.

The updated scheduler:

- Counts all acquisition reservations in the preceding 60 seconds, including
  scheduled refreshes, symbol changes, failed requests and unfinished attempts.
  SDK-internal HTTP retries do not consume separate reservations.
- Preserves the existing one-attempt-per-symbol-per-completed-five-minute-slot
  rule, completed-candle timing and five-second grace. Revisiting a symbol
  already attempted in that candle interval does not repeat the request.
- Allows a sixth new attempt when capacity expires, without waiting for the
  next candle boundary. Exactly 60-second-old reservations expire. It adds
  no fixed pacing delay, and clock-minute/candle boundaries do not reset it.
- Reuses the durable v1 reservation ledger across worker restarts. Existing
  entries retain their identity and count in the window; malformed reservation
  timestamps block further requests instead of silently freeing capacity.
- Records the new budget and window in new plans and the effective policy in
  worker status. A newly started updated worker reading a legacy plan without
  a window field uses the new five-per-minute default, rather than treating
  the old per-candle value of two as a minute budget. It preserves that plan.

**Rollout:** update the experimental checkout after ordinary session closeout
and use the new source for the next normal daily launch with `--overlay-ohlcv`.
An already-running worker retains its loaded scheduler. Restarting the viewer
cannot change that policy, and `restart-ohlcv` only recovers an ended worker;
it does not reload a healthy child. A forced worker termination or whole-session
restart is not required for this change. No new market-session qualification
stage is a prerequisite for continuing indicator development.

**Deferred collection metrics:** record transport-level attempts, intermediate
HTTP statuses/errors, SDK retry waits, final outcomes and aggregate request
rates across quote/OHLCV workers, with authentication accounted separately.
The current `request_count`/`http_requests` values count application calls,
including calls rejected locally by backoff, and omit SDK-internal retries.
Long SDK calls can include retry/token waits even when their final response
is successful. The proposed retry/throttling feedback loop is a hypothesis,
not an established cause of the previous slow acquisitions. This observability
work is an explicit deferred goal and does not gate the request-policy change
or indicator work.

**Verification:** the updated offline workspace suite passed **177/177 in
4.264 seconds**, with zero failures, errors or skips, using Dash 2.18.2 and
Plotly 6.5.2 in the isolated Linux candidate. Six additional regressions cover
rolling-window expiry and boundaries, persistence and malformed timestamps,
and actual worker selection handoffs for new and legacy plans. Network calls
and password prompts were blocked by the verifier. Windows execution of this
177-test candidate and its new live request allowance have not yet been
observed; the prior MasterBot visual evidence concerns the published
single-candle fix and existing acquisition/display integration.

## November 27 — restore live Nasdaq halt lines and detection diamonds

The operator observed missing first-observed diamonds for Nasdaq-admitted ZYN
and requested restoration of thick magenta halt lines. Inspection confirmed
three integration gaps: the live launch omitted the daily events input, the
viewer did not refresh halt evidence, and the daily loader rejected legitimate
HISTORICAL updates produced by the alternating Nasdaq collector.

The combined correction now:

- Automatically attaches the matching daily events file from the live data
  root and refreshes it on the existing live tick, independently of candles.
- Accepts both saved retrieval modes; waits for missing-at-start evidence and
  reports unavailable updates while retaining already accepted episodes.
- Restores a 7-pixel magenta line from Nasdaq's reported halt time at the first
  available in-halt quote price. First observation gates initial visibility;
  once visible, the line includes the earlier reported-start-to-detection gap.
- Gives every episode its own stable, timestamped Plotly legend entry. Lines
  can be hidden independently; diamonds remain a separate series; visibility
  persists when subsequent candles or episodes change trace ordering.
- Retains receipt history for reported trade-resumption changes. A line grows
  through the evidence clock while no endpoint is known and ends at a reported
  endpoint only after that update is known. Hover states when resumption has
  not yet been reported. It does not infer release from quote movement.
- Reads existing normalized captures for older session files, including today's
  collector output. New collectors persist endpoint history directly. Automatic
  Hot admission and Nasdaq request cadence are unchanged.

**Verification:** 200/200 offline workspace tests passed in 4.060 seconds with
zero failures/errors/skips, Dash 2.18.2 and Plotly 6.5.2. The suite now includes
the daily Nasdaq feed tests as well as live file evolution, same-symbol updates,
symbol-switch retention, independent halt receipt timing, resumption revisions,
backward seek, and per-episode renderer tests. Network and password prompts
were blocked throughout. A separate local Plotly.js 3.3.1 DOM rendering check
exercised actual legend hide/restore, diamond independence, and persistence of
a hidden episode through `Plotly.react` while a new episode remained visible.
This is not a claim of a new Windows desktop/live-market qualification.

**Rollout:** publish to the existing experimental branch under the standing
authorization. The current collector need not be restarted to use the viewer
fix: existing captures provide the receipt history. Preserve the previously
agreed checkout update after normal closeout; the next normal collector start
also picks up direct endpoint-history persistence and the already-published
five-per-minute OHLCV policy. Viewer-only restart loads the new display code.
No further live halt is required as a development gate. Indicator work and
deferred transport-level collection metrics retain their accepted priorities.
