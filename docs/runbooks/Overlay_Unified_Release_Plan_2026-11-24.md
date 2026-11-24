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

## Next project milestone after a usable Overlay

Propose **one end-to-end custom indicator prototype** on retained historical data: define inputs, calculation contract, visible chart/inspector output, reproducible out-of-sample evaluation and no-lookahead constraints. Indicator optimization and supervised execution will be subsequent explicit milestones. Avoid infrastructure expansion without a demonstrated dependency.

## Operator attention required next

- Start Nov 24 production session via the accepted one-unlock procedure and report controller status plus successful real quote acquisitions.
- Supply the promised Overlay screenshot/feedback; visual-specific UI changes should match the actual layout, not an inferred redesign.
- Keep production code, installed controller, credentials and live journals unchanged while experiment changes are reviewed.
