# Overlay Membership Operator bridge — synthetic experimental qualification

**Experiment only. No production integration, changes to main, or deployment.**

This branch starts from the accepted historical Overlay layout experiment
and brings the unchanged, independently Windows-qualified local operator
backend from `experiment/daily-membership-operator-ui-20261119`.
A new `SyntheticOverlayMembership` adapter refuses all state roots whose
session plan is not explicitly `operator_demo_synthetic=true` and bound to
the exact passed state directory. This is not a general-production operator.

## Behavioral contract

- Ordinary historical Overlay operation is identical: Historical remains
  selected by default and its three membership grids and inspector are
  read-only, causally tied to the replay clock.
- The Real Time option can be selected **only in the explicit offline demo**
  below. It is labeled synthetic QA, not a verified real-time data feed.
- In synthetic mode, the Membership tab uses the current effective membership
  of a disposable test journal. The existing preview/confirm/receipt backend
  is reused; the UI never writes the journal directly.
- Other workspace tabs are disabled in synthetic mode because their live
  adapters have not been qualified; historical replay is paused.
- The writer, HTTP server, and temporary journal are isolated from
  `daily_flow_candidate_20261106` and the production controller.
- Historical / Backtest request attempts are rejected server-side even if
  a client manipulates the UI.

## Windows offline QA

From a **separate checkout** of
`experiment/overlay-membership-bridge-20261119`, use the `dashing` conda
environment. Run:

```cmd
call verify_overlay_membership_bridge.cmd
```

This first runs the existing 72-test Overlay suite, then the unchanged operator
backend verification, then the new bridge tests. No provider calls or passwords.

For an interactive, disposable joint interface test, run:

```cmd
call preview_overlay_membership_bridge.cmd
```

Open `http://127.0.0.1:8062/`, select **Real Time (synthetic QA)**,
then **Membership**. Check INTC Hot → Focus → Hot via Preview, Confirm, and
worker-receipt effectiveness. Return to Historical to confirm the historical
data and selection are not rewritten. Use the existing **Stop server** action
to exit; the script stops the synthetic membership worker and removes the
temporary journal. Closing Firefox alone does not stop the server.

No claim of Windows acceptance is made until these commands and the desktop
interaction are verified on MasterBot.

## Compact Membership explorer refinement (November 20)

The Membership explorer uses four side-by-side panels on a wide desktop:
**Uni only, Focus only, Hot, and Symbol details**. The fourth panel contains
the selected symbol inspector and the one shared exact-symbol finder. The three
list grids now have **Symbol** and **Last** columns; the Name column and
individual floating filter rows are removed. The full available name and
saved quote details remain in the inspector. Searching is case-insensitive,
selects the highest exclusive tier at the current mode clock, and explicitly
reports a missing symbol instead of pretending it is admitted.

Each list initially sorts by symbol ascending. Clicking the Symbol header
switches between ascending and descending sorting without an additional
toolbar. The Last header also supports sorting. The synthetic operator's
Action and Destination dropdown controls use the same dark/light theme
variables as the existing Overlay replay dropdown. The Membership badge
displays an explicit synthetic-QA label when that mode is selected, rather
than the historical-only label.

The refinements are isolated to
`experiment/overlay-membership-bridge-20261119`; all journal and worker
semantics are unchanged. The new layout, search, and theme behavior need
MasterBot browser inspection in addition to Windows offline test results.

### Search-field editing and sorted-list navigation (November 20 refinement)

The fourth column now consists of **two separate surfaces**: a compact
**Symbol search** strip (ticker-only input plus Find) above a read-only
**Symbol details** card. The search input is deliberately narrower than the
description card.

When the ticker text changes, the previous selected symbol, inspector details
and the associated grid highlight are cleared. Selecting a symbol by clicking
one of the three tier lists still fills the search field and immediately shows
its details; the resulting programmatic search-field update does **not** clear
the selection. The bidirectional text/selection synchronization is implemented
in a **single Dash callback**, since Dash does not support a circular
dependency spanning separate callbacks. Enter and Find use the same exact, case-insensitive symbol
lookup against the current effective synthetic roster or historical replay
position. Unknown symbols report absence, never a fabricated membership.

Successful Find also outputs AG Grid `scrollTo` with
`{"rowId": SYMBOL, "rowPosition": "middle"}` **for the matching tier only**.
Each grid's existing `getRowId=params.data.symbol` makes the row identity
independent of current ascending or descending sort order; the other grids'
scroll positions are left alone. Search editing never changes the journal or
submits a membership action.

MasterBot Windows qualification now includes new tests for edit-to-clear,
programmatic symbol reflection, search strip layout and stable row-ID scrolling.
Actual browser scrolling under both sort directions still requires visual QA.

## Drag-and-drop membership proposals — November 20 experiment

**Synthetic Real Time only**. The existing three tier grids expose a
single-row drag grip in the Symbol column when the disposable demonstration is
active. Drag from a member's *current highest tier* to a different tier panel:

- Uni → Focus/Hot: ordinary promotion.
- Focus → Uni: ordinary demotion; Focus → Hot: promotion.
- Hot → Focus/Uni: ordinary demotion.

The three tiers remain exclusive on screen but nested in the journal. A drop
onto an external **HTML panel** sends a clientside-only
`membership-drag-request` event; it **does not mutate either grid's rowData,
queue a request, or publish a revision**. The synthetic
`SyntheticOverlayMembership.preview_drop` checks the user-supplied source
tier against the journal's current effective membership and infers the
promote/demote action. It then uses the already-qualified
`OperatorState.preview`; only a later explicit **Confirm and queue request**
can reach the existing worker inbox. The worker enforces membership policy,
capacity, and revision/effective-time guards.

The interface uses the documented Dash AG Grid
`dash_ag_grid.getApiAsync` + `addRowDropZone` API to register six
source/target pairs (two targets per tier). Source grids use a row-drag grip,
but the sorted lists **do not reorder locally**. Drop regions are highlighted
when hovered. The client JS can be disabled or torn down by changing back to
Historical, with `suppressRowDrag` restored; non-synthetic viewers never
register the binding. If JS binding fails, the original Preview button remains
available. This is a *single-symbol* workflow; multiple-row drag remains
disabled.

The experimental `src/mb_market_data/assets/membership_drag_bridge.js`
contains the clientside registration and drop routing, and
`src/mb_market_data/overlay_membership_bridge.py` performs authoritative
advisory checks before any preview is shown. The tests cover spoofed source
tiers, Historical-mode refusal, a no-write drag preview, and the unchanged
synthetic worker confirmation path. The JavaScript was syntax-checked and its
registration/drop/disable logic exercised with stubbed Grid API objects; this
does **not** replace a Windows browser test.

For interactive MasterBot QA: `call verify_overlay_membership_bridge.cmd`,
then `call preview_overlay_membership_bridge.cmd`; open
`http://127.0.0.1:8062/`, select **Real Time (synthetic QA)** and Membership.
Drag **INTC from Hot to Focus**, inspect the proposal, and verify the counts do
not change until Confirm + worker receipt effectiveness. Repeat **Focus to
Hot**, and try drag-to-Uni to test ordinary demotion. Reverse the symbol sort
before another drag; check that drag grips still work. Switch to Historical to
ensure dragging is disabled and historical inspection is unchanged.

**Accepted synthetic Firefox evidence:** Dragging INTC Hot → Focus highlighted
the target and created a preview without moving membership. Explicit Confirm
queued the shared worker request; the effective receipt led to the expected
Focus-only list membership (6 → 7) and Hot count (4 → 3). This confirms that
dragging works in the offline browser, not that live production is enabled.

## Find → Admit to Uni — November 20 experiment

Manual admission of a symbol **outside the entire Uni roster** is already
supported in the unchanged shared membership writer as an ordinary
`promote(symbol, target="uni")`. This patch exposes that capability in the
**offline synthetic Overlay only**; no new membership command type, publisher,
or journal update path was added.

In Real Time (synthetic QA), use the single **Symbol search** strip:

1. Find/Enter an exact ticker. An existing Uni/Focus/Hot member is selected
   normally; no admission offer is displayed.
2. If the ticker passes the worker's **canonical format check**, but is
   absent from the current effective roster, show a separate
   **outside-Uni / UNVERIFIED** card with **Preview admission to Uni**.
   No market-data request is made by the Find or preview operation.
3. Clicking this button infers Promote → Uni and calls
   `SyntheticOverlayMembership.preview_admission`. This verifies that the
   candidate still is *outside* the effective roster and invokes the existing
   `OperatorState.preview`. It does **not** enqueue a request or change a list.
4. The operator reviews the preview at the existing controls and clicks
   **Confirm and queue request**. The existing membership worker checks the
   source, session/revision freshness, the configured `membership_max_extra`
   capacity (40 by default), and **exact provider EQUITY identity for a new
   symbol** before publishing a future-effective hierarchy revision.
5. The token enters Uni only after the revision becomes effective. Focus/Hot
   are unchanged. Once Uni contains the symbol, Find selects it normally and
   the outside admission card automatically hides on the receipt refresh.

The frontend **never claims that matching ticker syntax proves a valid
security**. It only offers review. Actual production identity verification
requires the authenticated membership worker, via
`daily_membership.verify_identity`, and its `assetMainType == 'EQUITY'` check.
The disposable demo uses an explicitly simulated identity response with no
Schwab connection. Beginning with the November 22 QA improvement, it uses an
explicit allowlist of positive test cases. `AMD` and `T` are accepted fixtures;
`NOTREAL` and `REALLY` are deliberately rejected. **No synthetic admission
result is evidence that a ticker is genuinely listed or is recognized by
Schwab.**

The card is hidden or disabled in Historical mode and when its candidate is
already admitted or the worker is unavailable. Editing the search input
clears the admission offer and invalidates any outstanding manual preview,
so the Confirm button cannot silently apply an unrelated stale action.
Ordinary drag-and-drop between the three existing tiers is unaffected.

**Acceptance tests:** run `call verify_overlay_membership_bridge.cmd`, then
`call preview_overlay_membership_bridge.cmd` in the isolated integration
checkout. In Firefox, Real Time (synthetic QA) → Membership → Find `AMD`;
verify the unverified admission card, click Preview admission, confirm
explicitly, and observe AMD in Uni only. Try `NOTREAL`: syntactically
well-formed, but the disposable worker should return `REJECTED` and it
must remain outside all tiers. Ensure Find of existing `INTC` has **no** (e.g. `$$$`) and Historical mode.

Only isolated worker/browser qualification is covered here; *this does not
authorize enabling actual live production operator controls*.

## November 22 refinement — one current transaction, visible history cleared

After November 22 synthetic Firefox QA, the operator reported that Preview
and Confirm status messages could refer to **different prior commands**.
A new preview changed the preview message without removing the earlier queue
message and successful receipt. The following experimental corrections address
that UI confusion:

- Preview (manual, drag, or outside-Uni admission) **atomically** clears the
  old command ID and replaces the queue and receipt messages with explicit
  "no request queued / no receipt for this preview" messages.
- Confirm produces a new queue message containing the current symbol, target
  and command ID. Only this command is subsequently polled for worker receipts.
- Worker receipt messages identify the symbol and command ID. Rejections and
  failures include the worker's stated reason; effective receipts distinguish
  successful publication from the earlier advisory preview.
- Changing Find to a different symbol invalidates the preview, command ID
  and old status displays. Programmatic reflection of the **same**
  just-admitted symbol does not accidentally erase the successful transaction.
- The Confirm button becomes disabled after the current preview has already
  been submitted, until a new preview is prepared.

These are presentation and callback-ownership changes in synthetic Dash, not
changes to membership-writer semantics, idempotence, capacity policy, or
authority to publish.

## November 22 refinement — automatic effective-transition selection

After an effective admission **or any confirmed promotion/demotion**, the
synthetic bridge now checks:

1. The worker receipt is `EFFECTIVE_AS_PUBLISHED` for the current command.
2. The effective membership snapshot reports the proposed destination tier.
3. The ticker is present in that destination tier's current AG Grid `rowData`.
4. This exact command ID has not already triggered automatic selection.

Then it highlights the symbol in its new effective tier, clears stale
highlights elsewhere, centers the destination list by stable symbol ID, and
updates the search/inspection status. A completed Hot→Focus, Uni→Focus,
Focus→Hot, etc., now behaves like an outside→Uni admission.

At **drop time**, before any worker publication, the Find field is also
synchronized to the dragged symbol, the source row stays highlighted, and
the old symbol's search/admission message is cleared. A programmatic Find
reflection caused by dragging must not invalidate the current Preview;
a later genuine manual search edit still clears stale Preview/Confirm state.

For the post-effective update, the Find field retains its already
synchronized ticker rather than being overwritten by a second callback.
The update happens **once per command**, so later refreshes do not override
subsequent manual list choices. Failed requests, pending revisions, missing
destination grid rows, and Historical mode never trigger selection.

The deliberate wait for the destination `rowData` avoids the observed
rendering race between receipt effectiveness and grid scrolling. The
inspector may show **Unavailable** quote fields until an actual acquisition;
it never fabricates quotes.

## November 22 refinement — controlled synthetic identity fixtures

The demo's worker formerly accepted every syntactically valid ticker except
`NOTREAL`. It now **accepts only** these deliberately approved synthetic
test fixtures:

`AMD`, `T`, `AAPL`, `AMZN`, `GOOG`, `META`, `TSLA`, `PLTR`, `SOFI`.

Unlisted names such as `NOTREAL`, `REALLY`, or `ZZZZ` are rejected with
`SIMULATED: Exact equity identity not confirmed`; the proof artifact explicitly
reports synthetic identity evaluation and `provider_calls: 0`.
No live symbol directory is used. The production code
`daily_membership.verify_identity` remains unchanged, and requires exact
Schwab `EQUITY` recognition when the live worker is separately authorized.

## Deferred enhancements — manual admission eligibility policy

**Deferred; no implementation or production gate change in this experiment.**
Beyond the current mandatory exact Schwab `EQUITY` identity check, consider
adding configurable eligibility restrictions for manual outside-Uni admission:

- An approved primary listing venue or exchange, as distinct from a provider
  merely recognizing an instrument;
- OTC or non-approved venue exclusions;
- explicit equity instrument/subtype policy (e.g., ADRs, preferred shares,
  warrants, funds/ETFs, rights or units), based on supported, verified
  provider metadata rather than name heuristics;
- active/current listing or tradability status (and the meaning of trading
  halts vs security ineligibility);
- operator-visible eligibility evidence and clear rejection reasons.

**Do not silently treat these ideas as current requirements.** Their exact
values, data sources, edge cases, and compatibility with the daily Universe
selector must be specified and approved before implementation. In particular,
a market halt by itself is not evidence that a genuine listed security should
be excluded, and ordinary halted-symbol demotion remains the accepted current
policy. Revisit this only during production operator readiness or if actual
identity data reveals a gap in the existing rule.

## Remaining production gates

The isolated synthetic controls are not an approved production operator.
Remaining gates include a real-time read adapter for other workspace tabs,
localhost origin/CSRF controls and installed-controller path validation,
separate attended live membership QA, and explicit operator deployment
approval. No main merge or production session change is authorized here.
