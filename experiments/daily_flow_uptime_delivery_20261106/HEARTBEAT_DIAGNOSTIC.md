# Optional real-time heartbeat diagnostic

The operator's November 6 controller log was empty. The subsequent System and
Application queries reported no matching events around 10:11:09, 10:36:17,
12:48:28 and 13:24:14 Eastern time (two minutes on either side). No access error
was reported. These results neither identify the shutdown cause nor rule out
brief file-sharing conflicts or scheduling stalls. The old worker logs did not
record the precise stop condition.

`heartbeat_compare.py` is a separate observation tool, not a polling release
change or a prerequisite for Monday's planned session. It reads three pinned
source files from the original and corrected candidates, without importing the
market-data runtime or authentication packages. It writes only in a new dated
subdirectory of `%USERPROFILE%\hbdiag`. It does not read real owner files,
credentials, token databases, journals, or Monday's opening proposal.

One foreground parent uses the delivered atomic heartbeat writer every two
seconds. Two spawned diagnostic readers check every 0.25 seconds, using actual
UTC and monotonic time. For each check the original decision and corrected guard
receive the same first actual read result or exception. Corrected retries make
new physical reads. No faults, locks, clock jumps or broker activity are injected.
The original logic is observed after its first would-stop condition to collect
more evidence; the corrected guard's actual stop remains latched and ends the
experiment. This does not reproduce the whole original controller workload,
SQLite activity, quote requests, or the exact filesystem location/timing of the
November 6 incident.

Run from any CMD directory in the existing closeout-qa environment:

```cmd
git -C "%USERPROFILE%\uptime_delivery_20261106" pull --ff-only && python -B "%USERPROFILE%\uptime_delivery_20261106\experiments\daily_flow_uptime_delivery_20261106\heartbeat_compare.py" --minutes 60
```

Progress is printed each minute. Keep the computer awake and use it normally;
no security-setting changes are requested. Ctrl+C requests cleanup of this
script's children and saves a partial result. Closing the console forcibly or
rebooting cannot guarantee a final report. Files and diagnostics already flushed
remain in the run directory. Do not alter the source hashes or delete candidate
folders when preflight refuses a mismatch.

The final report and CSV/JSONL details retain actual read errors (type, errno,
winerror), heartbeat timestamps, reader timing gaps, writer errors, and both
policies' decisions. Exception messages and secrets are not recorded.

Possible outcomes:

- NO_STOP_CONDITION_OBSERVED: not reproduced within this run, not proof of a fix.
- OLD_STOP_CONDITION_OBSERVED_FIXED_CONTINUED: the old decision rejected a read
  while the corrected guard recovered/continued; inspect the captured cause.
- CORRECTED_STOP_OBSERVED: inspect whether the owner really expired, the writer
  failed, or another condition occurred. This is not automatically a guard bug.
- INTERRUPTED_NO_STOP_OBSERVED: intentionally stopped before the duration limit.
- DIAGNOSTIC_INCOMPLETE: diagnostic/child failure or missing results; not a pass.

All outcomes explicitly retain original_live_shutdown_cause_proven=false and
live_uptime_qualified=false. Even a naturally observed read conflict here cannot
prove which condition occurred in the old uninstrumented November 6 run.

Local validation before publication: four harness checks passed (same-first-read
fault logging/recovery, genuine expiry/latching, output/source preflight, outcome
classification). Two short Linux actual-process runs completed, including one
12-second run; a separate actual SIGINT/child-cleanup run retained its partial
result. Faults were injected only inside the harness check, not in the real-time
runs. This new diagnostic has not yet run on Windows or for its full 60-minute
default. No rerun of the accepted 177-test correction suite is required to run
this separate diagnostic. The 129-file corrected candidate and its published
reconstruction payload are unchanged.
