# Daily CMD status viewer — automatic return and Q-key fallback

**Scope:** experimental source change only, November 25, 2026, in
`DanArizona/mb_market_data:experiment/overlay-unified-release-20261124`.
The installed daily controller in MasterBot's production QA folder,
GitHub `main`, and already-running November 25 viewer are **unchanged**.

## Original symptom and cause

On several Windows 11 MasterBot Anaconda CMD sessions, the foreground
`daily.cmd start --live ...` command continued showing
`DAILY SESSION ... | FINISHED` and never returned to a CMD prompt.
Ctrl+C did not reliably interrupt the viewer.

The batch file invokes `python -u -B probes/run_daily_session.py %*`
and returns to CMD only when Python exits. The daily controller runs
independently as an owned child. On successful startup, the original
foreground launcher called `watch(root)`, whose unconditional
`while True` had no final-status exit and relied on Ctrl+C exclusively.

This establishes the specific reason the CMD prompt stayed occupied
**after FINISHED**. The cause of Ctrl+C itself being unreliable in
Anaconda CMD is **not established**; do not assert that Conda initialization
caused it. The Dashboard's `Stop server` is a different local WSGI
process and cannot close the daily-session status viewer.

## Experimental source correction

1. The foreground `start --live` launcher now uses
   `watch(root, exit_when_final=True)`. It returns to CMD only after
   reading a verified `final_status=true` receipt from the
   completed controller's cleanup and a terminal `FINISHED`/failure
   phase. A transient `FINISHED` before final cleanup is not enough.
2. Operators can press **Q** in the Windows CMD status viewer to close
   the foreground viewer at any time without depending on Ctrl+C;
   `msvcrt.kbhit()/getwch()` provides a nonblocking console input
   check. This does not issue stop/exit requests or touch controller
   processes, journal files, or credentials.
3. The explicit `daily.cmd watch --state-root ...` remains a persistent
   status watcher until Q/Ctrl+C, and the `status` command remains
   one-shot. Only the viewer attached to the attended `start`
   auto-returns after final cleanup.
4. `tests/test_daily_viewer_lifecycle.py` checks final receipt
   auto-return, no early exit on phase alone, Q-only viewer exit
   without altering status, and wiring of the attended launcher.
   Offline suite expects **165 tests**, including these four.
   MasterBot Windows verification and any production promotion
   remain outstanding.

## Immediate November 25 workaround

The viewer that was already started from the installed controller
**cannot pick up these code changes**. Once the independently
running controller has published a `FINISHED` final report with all
worker exits, safely close **that specific old foreground CMD
window using its X button**, as done on November 20/24. This is not
a production shutdown command. Do not infer that a saved independent
monitor status shows a currently running process; final status is
historical evidence only.

No changes to the production launcher or `main` without explicit
authorization.
