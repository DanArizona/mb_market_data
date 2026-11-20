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

Successful Find also outputs AG Grid \`scrollTo\` with
\`{"rowId": SYMBOL, "rowPosition": "middle"}\` **for the matching tier only**.
Each grid's existing \`getRowId=params.data.symbol\` makes the row identity
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

**Remaining gate:** real drag events and cursor behavior at 90% Firefox
magnification require explicit Windows user acceptance. No production or main
change is implied.

## Remaining production gates

A real-time read adapter for the other workspace tabs, reviewed localhost
CSRF/session origin rules within Dash, installed-controller path validation,
attended live membership QA, and separate operator deployment approval.
No main merge or production changes are authorized by this experiment.
