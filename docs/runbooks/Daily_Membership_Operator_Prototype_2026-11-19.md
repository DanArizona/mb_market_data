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

## Separate Windows QA only — when approved for an operator trial

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

Run offline tests without any provider access or password:

```cmd
python -B -m unittest -v tests.test_daily_operator_ui
```

## Explicit limitations and next gates

- This prototype is **not** installed, exposed inside the historical
  Membership tab, or qualified for a production session.
- The separate historical Overlay stays read-only; no drag-and-drop, trading,
  or remote membership editing is added.
- Server-side previews are advisory. The existing membership writer is the
  final authority on caps, source policy, equity identity, effective time,
  and whether the request is accepted.
- Before an attended Windows live operator trial: run the integrated offline
  verifier in a clean test environment; confirm the installed controller plan
  and state path; review localhost access and stop behavior; and approve the
  exact deployment/launch procedure. Do not replace installed production code
  or merge to main as part of this prototype.
