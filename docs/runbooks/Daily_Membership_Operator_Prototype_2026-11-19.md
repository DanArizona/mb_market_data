# Daily membership operator prototype — 2026-11-19

**EXPERIMENTAL. No production deployment, main merge, or installed-controller change.**

This is an additional **local-only** browser window for a session which was
already started with `membership_control=daily-shared-membership-v1`. It does
not enable membership controls on an existing session, start a collector,
request credentials, or call the market-data API.

## What the prototype does

- Displays effective-at-current-time Uni-only, Focus-only, and Hot rosters
  (one symbol per list) using the existing schema-v2 journal opened read-only.
- Inspects a selected symbol's latest saved quote, showing the individual
  provider quote time separately from API response and acquisition-completion
  timestamps. Missing fields remain missing; estimates are labeled.
- Disables actions unless the existing controller, opted-in plan, collecting
  window, and membership worker are current. It blocks a preview while a
  published hierarchy revision is waiting to become effective.
- Offers a two-step operation: **Preview** (no write), then explicit
  **Confirm and queue**. At confirmation it rechecks session identity and
  published revision, then calls the existing `daily_membership.submit`.
- Displays the command ID and follows the worker receipt through PENDING,
  PUBLISHED_PENDING_EFFECTIVE, EFFECTIVE_AS_PUBLISHED, REJECTED, or FAILED.
  Publication is never misrepresented as immediate effectiveness.
- Binds exclusively to `127.0.0.1`, requires same-origin JSON and an
  unpredictable session token for every command POST, and has no direct
  journal-write path. Repeated confirmation of the same proposal does not
  queue a second command.

Ordinary demotion is the accepted policy: a previously observed halt episode
will not automatically undo it; a distinct later qualifying Nasdaq halt can
elevate the symbol again. `Suppress Automatic` remains deferred.

## First Windows QA: fully isolated synthetic demo

The experiment branch includes a **self-contained synthetic demonstration**.
It creates a brand-new temporary state directory and schema-v2 journal,
fabricates Uni/Focus/Hot memberships and quotes, and runs the **existing
membership worker** with an explicitly fake identity verifier. The web page
uses the normal Preview → Confirm → worker receipt → effective-revision path.
Neither the UI nor the worker constructs a Schwab client, loads credentials,
connects to a market-data service, or touches production session files.

Use a *separate* checkout of
`experiment/daily-membership-operator-ui-20261119` on MasterBot. In Windows
CMD, activate the offline QA environment, change into that checkout, then run
each command separately:

```cmd
conda activate closeout-qa
```

```cmd
call verify_membership_operator.cmd
```

This verifies both the prototype's offline unit tests and a full local HTTP
preview/confirm/worker/effectiveness exercise. After it passes, start the
synthetic viewer (default port 8054):

```cmd
call membership_operator_demo.cmd
```

Open **http://127.0.0.1:8054/** in Firefox on the same computer. Confirm the
prominent amber **OFFLINE SYNTHETIC DEMO** banner, and try:

1. Select `INTC` in Hot. Choose **Ordinary demote → Focus**, Preview, examine
   the proposed change, then Confirm. Watch the receipt until the revision
   becomes effective; `INTC` should then appear in Focus only.
2. Select `INTC` again and **Promote → Hot** through a new Preview/Confirm.
   Observe the second revision and its eventual effectiveness.
3. Select another member to inspect its synthetic quote timestamp, distinct
   from the fabricated API response time. Try Preview then Cancel and confirm
   that no request is queued.
4. Optionally enter an outside-Universe symbol. Its identity validation in
   this demo is **simulated**, not an actual Schwab equity check.

The runner prints the local URL and does **not** launch a browser. The demo
starts near the current wall clock so that its future-effective revisions can
be observed without seeking. Closing the Firefox tab does not stop the demo;
use **Ctrl+C in its own CMD window** to stop the synthetic worker and server.
The temporary journal and receipts are discarded automatically when the
process exits. No production worker, installed controller, or main branch is
modified.

If port 8054 is occupied, pass `--port 8055` to the demo launcher and use
`http://127.0.0.1:8055/` instead. If a test fails, leave production alone and
capture the CMD output for diagnosis.

## Later Windows QA against an existing opted-in session — not yet approved

From an isolated integration checkout with the appropriate Python environment:

```cmd
conda activate closeout-qa
```

Inspect the existing session's **actual state root** before running the next
command. The path below is illustrative, not an assumed live deployment path.

```cmd
call membership_operator.cmd --state-root "C:\PATH\TO\EXISTING\daily_session\2026-11-19" --port 8054
```

Open `http://127.0.0.1:8054/` on that computer. Closing the viewer or using
Ctrl+C in its console stops only the operator window; collectors keep running.

The old direct test command below is superseded by the dedicated verifier
above, which supplies the checkout's `src` and `tests` paths:

```cmd
call verify_membership_operator.cmd
```

## Explicit limitations and next gates

- This prototype is **not** installed, exposed inside the historical
  Membership tab, or qualified for a production session.
- The separate historical Overlay stays read-only; no drag-and-drop, trading,
  or remote membership editing is added.
- Server-side previews are advisory. The existing membership writer is the
  final authority on caps, source policy, equity identity, effective time,
  and whether the request is accepted.
- Before an attended Windows *live* operator trial: accept the synthetic QA
  results, confirm the installed controller plan and state path, review
  localhost access and stop behavior, and approve the exact deployment/launch
  procedure. Do not replace installed production code or merge to main as
  part of this prototype.
