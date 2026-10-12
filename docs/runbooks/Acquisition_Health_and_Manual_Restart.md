# Acquisition health and guided manual restart — v1

This increment observes the existing schema-v2 Uni and Focus pollers from a
separate process. It can be accepted while production polling continues. It
does not patch or replace the running pollers, install a service, or perform an
automatic restart. Hot polling is outside the current scheduler contract.

## First acceptance on MasterBot

Extract the ZIP's contents directly into:

```text
C:\Users\danla\Documents\github\mb_market_data\output\polling_health_recovery_v1
```

The file `01_prod_main_offline_verify.cmd` must be directly inside that folder.
In **prod-main**, with **sea-green** active, run:

```cmd
call "C:\Users\danla\Documents\github\mb_market_data\output\polling_health_recovery_v1\01_prod_main_offline_verify.cmd"
```

There is no password prompt, API request, production-journal read, or real
poller launch in this verification. It tests against synthetic journals using
the reviewed production store source. On Windows it additionally checks CIM
process inspection, argument parsing, and the native operation mutex. Expected:
**Offline polling-health verification: PASS**. Send the final summary before
launching the observer. A PASS marker is required by the later launchers.

## Start independent monitoring

After offline acceptance, open another CMD window, activate sea-green, and run:

```cmd
call "C:\Users\danla\Documents\github\mb_market_data\output\polling_health_recovery_v1\02_prod_health_monitor.cmd"
```

This names the window **prod-health**. Enter the session date when prompted;
the default is the current Eastern date. For October 7, enter `2026-10-07`.
Leave the window open. No password is required. Keep **prod-poll-uni** and
**prod-poll-focus** running normally. Stopping this monitor does not stop those
independent pollers.

Stop the monitor by pressing **Q** in prod-health. Clear any CMD text selection
with Esc first. Ctrl+C is also supported, but its physical Windows keyboard
behavior remains unconfirmed on MasterBot. If the console does not respond,
request a monitor stop from prod-main:

```cmd
call "C:\Users\danla\Documents\github\mb_market_data\output\polling_health_recovery_v1\05_prod_main_stop_monitor.cmd"
```

Enter the matching session date. This writes only a monitor stop request;
independent pollers continue. The monitor records its shutdown in last_stop.json.

The monitor checks every five seconds, plus the time needed for inspection.
An explicit journal error or missing process does not wait for slot grace.
Scheduled slots become overdue after the configurable 90-second default grace;
the grace allows request/commit time and is not an acquisition retry policy.
Market-quote and trade timestamps are never used as a poller heartbeat.

The observer opens short, read-only SQLite snapshots, including committed WAL
content. It reads acquisition headers and the latest outcomes, not every
historical quote. It never checkpoints, initializes, or writes journal data.
Read contention/failure creates a persistent alert; inspection is retried on
the next cycle. It cannot identify which process owns SQLite's writer lock.

Each channel shows state, candidate PID(s), latest committed slot, counts of
acquisitions and empty-membership skips, current membership, saved errors, and
missing scheduled slots. A committed acquisition containing request-error
outcomes raises a separate data alert. Historical coverage gaps remain visible
even after acquisition resumes. A normal finish after the final configured slot
(Uni 15:59:30, Focus 15:59:50) is SCHEDULE_COMPLETE before 16:00 and WINDOW_ENDED
afterwards. Missing final commits and genuine early exits still raise alerts.

Operational records are written outside the journal:

```text
output\polling_health\SESSION_DATE\
    STATUS.txt       current readable status
    latest.json      machine-readable current state, for future Overlay integration
    incidents.json   persistent incident history and resolution times
    events.jsonl     first detections
```

Unresolved failures show a large red window that stays on top of other windows.
Three tones repeat every ten seconds. The window shows the latest condition
and check time rather than queuing old popup messages. Its title flashes, and
an unsilenced alarm brings a minimized window back when the sound repeats.
Sound and window workers operate independently of journal/CIM inspection.

**Silence for 5 minutes** pauses the sound while monitoring continues. A new
failure immediately cancels that pause. Closing a red alarm window also
silences it for five minutes and keeps it visible; an unresolved failure cannot
be dismissed. Sound stops only after the current urgent condition clears,
including a new committed slot when confirming restart. A quiet notice stays
visible until you dismiss it, so an unattended failure remains visible after
recovery. Historical coverage gaps do not produce a repeating urgent sound;
they remain in the window, STATUS.txt, and incident history.

Click the prod-health CMD window before pressing Q to stop the monitor, or use
the separate 05 stop launcher. Silencing the alarm does not stop monitoring.
The monitor must remain running; service-level supervision is future work.
`--no-popup` suppresses the desktop window and sound when explicitly requested.

The window uses Python's supplied Tk desktop runtime; the offline verifier
checks it silently on Windows. No extra pip package is required. To verify the
intrusive window and sound without a production journal, API call or password,
run this in prod-main:

```cmd
call "C:\Users\danla\Documents\github\mb_market_data\output\polling_health_recovery_v1\06_prod_main_alarm_demo.cmd"
```

This first runs 47 offline checks and the native process/mutex/Tk checks. It
then shows an explicitly synthetic failure for 40 seconds. Listen for repeated
three-tone alerts or try the five-minute silence button. Simulated recovery
stops the sound and leaves a quiet notice; dismiss it to finish. Q in that CMD
window also stops the demonstration. It stops automatically after five minutes.
The demonstration writes only its own reports under output/polling_alarm_demo.

A blank Windows process command line is rechecked within the inspection's
shared eight-second deadline. A PID is excluded only if the second query
confirms it is absent. A still-present unreadable process, or a failed recheck,
continues to block restart. This avoids assuming that every blank entry is an
exiting process.

## During a polling failure

1. Read the affected poller's CMD error and the monitor's saved-error details.
   Leave healthy channels running. Retain every evidence directory.
2. Confirm the failed poller has returned to CMD. Do not launch another copy
   over a still-running or hung process. This packet never kills processes.
3. In the affected channel's existing CMD window, with sea-green active, run
   the matching guided restart launcher:

```cmd
rem In prod-poll-focus, only when Focus has stopped:
call "C:\Users\danla\Documents\github\mb_market_data\output\polling_health_recovery_v1\04_prod_poll_focus_restart.cmd"
```

```cmd
rem In prod-poll-uni, only when Uni has stopped:
call "C:\Users\danla\Documents\github\mb_market_data\output\polling_health_recovery_v1\03_prod_poll_uni_restart.cmd"
```

4. Enter the session date. The tool verifies the reviewed source, active
   09:30–16:00 ET window, readable journal, and absence of a candidate poller
   for that channel. Unknown/unreadable process inspection refuses the launch.
   Windows does not expose a poller's working directory through CIM; matching
   relative paths are conservatively treated as candidates and block restart.
5. Before launch, it briefly attempts a write reservation with a 250-ms wait
   and immediately rolls back, without inserting/changing any records. If the
   writer lock remains blocked, it refuses to start and prints the reason.
   Report that message; do not repeatedly launch pollers into a persistent lock.
6. The existing production poller starts in this window and prompts for the
   encrypted-config password. It appends to the **same original daily journal**,
   using a new run record and evidence folder. The healthy channel is untouched.
7. Confirm new samples and successful commits. The monitor records recovery
   only when the replacement run commits a scheduled acquisition or legitimate
   empty-membership skip; a new PID alone is insufficient.
   Before the replacement's first scheduled slot is due, its state is
   AWAITING_COMMIT. Earlier gaps remain a separate coverage alert. If the new
   run misses its own due slot after grace, it becomes OVERDUE.

A restart request receipt is saved under
`output\polling_restart_requests\SESSION_DATE\...\request.json`. It records the
pre-launch health, exact command, and eventual child exit. An OS-owned mutex
blocks simultaneous guided restarts and releases after a crash or exit. The
process check also refuses an existing manually launched poller. An unrelated
operator can still race the tool by manually issuing a raw launch command;
operators should use this wrapper consistently. No retry or restart is automatic.

The reviewed launcher targets the established production command defaults:
all quote fields, batch size 400, and timeout 20 seconds. It preserves the
selected session/window and original journal root. Custom poller configurations
need review before using this packet.

## After the polling window ends

Use `manual_quote_recovery_v1` for any saved acquisitions missing from the
original journal after all configured polling windows and runs have finished.
That tool creates a preserved original and a separate recovered analysis copy
from evidence across all runs. Restart and salvage have separate roles:
restart limits future loss; salvage restores only evidence actually acquired.
Unacquired slots remain gaps. No recovery snapshots need to be merged manually.

Run the normal final production audit/replay as well. A responsive channel or
a verified salvage does not establish complete full-day coverage.

## Validation and limits

The current local check passed 47 focused failure/recovery/alarm tests and 66
store, membership, and polling-journal checks (113 total). A full-size synthetic
October 6 journal was correctly classified as Uni 780/780 with no gaps and
Focus 1,506/1,560 with 54 gaps, retaining the recorded lock error. Its read took
approximately 14 ms locally; this is not a Windows latency guarantee.

The general full test-suite attempt in the default local Python environment
was blocked by missing pre-existing frontend and mb_tools dependencies (Dash,
Plotly, Werkzeug, mb_tools). The relevant dependency-free contract suite passed.
These tools add no dependencies to sea-green. MasterBot passed all 28 packet
checks and native process/mutex inspection on October 7. The completed real
October 7 journal was correctly classified with Uni 780 and Focus 1,560
acquisitions, no missing slots, and both final slots committed; its full audit
and replay also passed. Ctrl+C did not stop the original monitor on that
attempt; the operator closed its window. Q and the explicit stop-request
launcher are now included in the production ZIP. Updated native process
inspection passed a 39-check Windows verification on October 7. The updated
production monitor then correctly read the complete journal and stopped with
Q. The intrusive desktop alarm is a later increment and still needs its
47-check verification and on-machine window/sound demonstration. An actual
API restart has not been performed by these tools.

### Isolated real-process test

`polling_health_live_test_v1` is a separate extracted ZIP folder. Its synthetic session
script uses reviewed journal code, generated fake quote outcomes, an exclusive
case directory, and a generated synthetic probe. No production database is
opened and the production API probe is never executed. It uses the same 47
focused checks and current health/restart/alarm code as the production ZIP.

The packet opens `test-poll-uni`, `test-poll-focus`, and `test-health`. The
operator triggers a Focus failure, confirms continued Uni commits and a
missing Focus slot, and requests a guided restart using the same mutex,
duplicate inspection, write-availability probe, and receipt code. Recovery
requires a newly committed acquisition in the same synthetic journal. The
intentional failure request targets the original run exclusively; it cannot
stop the replacement. No real SQLite lock is injected.

The test monitor supports Ctrl+C, Q, and a separate stop request, and records
its shutdown reason outside the journal. Explicit signal handlers are restored
on exit. Special console keys are discarded before checking Q. These controls
are included in both ZIPs. MasterBot's October 7 synthetic process test passed:
Focus failed, Uni continued, a new Focus run committed into the same journal,
17 earlier missing Focus slots remained visible, Q stopped the monitor, and
the beep was confirmed. All synthetic processes were then stopped. The
intended failure popup and Ctrl+C were not confirmed; other alert popups
appeared. The incident log recorded two temporarily unreadable process entries
and a false OVERDUE state while the new Focus run awaited its first slot.
The correction rechecks blank process entries and waits for that new run's own
first due slot. It passed local automated and process checks, followed by
MasterBot's 39-check verification and production monitor/Q check. The new
persistent alarm window and repeated tones still require desktop acceptance.

For the next source increment, see [Polling resilience deployment](Polling_Resilience_Deployment.md).
It adds bounded writes, exact alarm commands, an inspector watchdog and the
Overlay health panel. Those changes require candidate verification and a
separate after-close installation; extracting a ZIP does not update pollers.

The original lock's owner/cause remains unresolved. This increment preserves
diagnostic timing, SQLite error text, process candidates, and slot loss without
claiming a root-cause fix. Bounded write retries, automatic recovery, Overlay
health display, and an always-on supervisor remain later increments.

## Monitor status-file publication and watchdog incident history

Windows may briefly deny replacement of a status file while another process has
it open. Status publications retry Windows errors 5, 32 and 33 for at most 0.5
seconds and eight attempts. The old complete status file remains in place until
replacement succeeds. Other I/O failures are reported without retry.

If publication still fails, the inspector/watchdog continues operating. A red
monitor alarm reports that saved status may be stale; this does not call for a
poller restart. The next successful status publication resolves that condition.
If no completed inspector snapshot is visible, the watchdog's normal overdue
check remains active. A stop-receipt write error is printed without preventing
Q or Ctrl+C shutdown.

Watchdog STOPPED/OVERDUE and status-output incidents are retained separately in
`output/polling_health/<session>/monitor_incidents.jsonl`. This append-only history
includes detection, confirmed resolution and acknowledgement. Restarting a
watchdog cannot establish recovery until its replacement inspector publishes a
completed check. Resolved, unacknowledged monitor incidents reappear as quiet
notices across restarts. Dismiss acknowledges them; unresolved alarms cannot be
dismissed. History-read/write failures are explicit monitor alarms.

This addresses the October 9 Access-denied failure replacing `supervisor.json`;
the process that prevented replacement has not been identified. It does not
change polling cadence, journal contents, SQLite retry budgets or membership.
