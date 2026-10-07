# Acquisition health and guided manual restart — v1

This increment observes the existing schema-v2 Uni and Focus pollers from a
separate process. It can be accepted while production polling continues. It
does not patch or replace the running pollers, install a service, or perform an
automatic restart. Hot polling is outside the current scheduler contract.

## First acceptance on MasterBot

Extract the packet's contents directly into:

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
even after acquisition resumes.

Operational records are written outside the journal:

```text
output\polling_health\SESSION_DATE\
    STATUS.txt       current readable status
    latest.json      machine-readable current state, for future Overlay integration
    incidents.json   persistent incident history and resolution times
    events.jsonl     first detections
```

New alerts generate a Windows message box and sound independently of the
Overlay. The message box uses a separate thread, so leaving it open does not
block inspection. Active conditions produce an audible reminder every minute.
`--no-popup` suppresses these desktop alerts when explicitly requested.
The console and incident files remain available after a poller exits. The
monitor itself must remain running; service-level supervision is future work.

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

The initial local check passed 26 focused failure/recovery tests and 110
combined polling, membership, and journal contract tests. A full-size synthetic
October 6 journal was correctly classified as Uni 780/780 with no gaps and
Focus 1,506/1,560 with 54 gaps, retaining the recorded lock error. Its read took
approximately 14 ms locally; this is not a Windows latency guarantee.

The general full test-suite attempt in the default local Python environment
was blocked by missing pre-existing frontend and mb_tools dependencies (Dash,
Plotly, Werkzeug, mb_tools). The relevant dependency-free contract suite passed.
These tools add no dependencies to sea-green. Native Windows process/alert and
real viewer/poller acceptance remain pending on MasterBot. An actual API
restart has not been performed by this packet during local validation.

The original lock's owner/cause remains unresolved. This increment preserves
diagnostic timing, SQLite error text, process candidates, and slot loss without
claiming a root-cause fix. Bounded write retries, automatic recovery, Overlay
health display, and an always-on supervisor remain later increments.
