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

## Next project milestone after a usable Overlay

Propose **one end-to-end custom indicator prototype** on retained historical data: define inputs, calculation contract, visible chart/inspector output, reproducible out-of-sample evaluation and no-lookahead constraints. Indicator optimization and supervised execution will be subsequent explicit milestones. Avoid infrastructure expansion without a demonstrated dependency.

## Operator attention required next

- Start Nov 24 production session via the accepted one-unlock procedure and report controller status plus successful real quote acquisitions.
- Supply the promised Overlay screenshot/feedback; visual-specific UI changes should match the actual layout, not an inferred redesign.
- Keep production code, installed controller, credentials and live journals unchanged while experiment changes are reviewed.
