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

## Remaining production gates

A real-time read adapter for the other workspace tabs, reviewed localhost
CSRF/session origin rules within Dash, installed-controller path validation,
attended live membership QA, and separate operator deployment approval.
No main merge or production changes are authorized by this experiment.
