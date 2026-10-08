# Polling resilience: write recovery, monitoring and display

This increment adds bounded journal transaction recovery, structured write
diagnostics, exact restart instructions, an independent inspector watchdog,
and a read-only acquisition-health panel above the Overlay's tabs.

## Verification before installation

Extract `polling_resilience_v1.zip` into:

```text
C:\Users\danla\Documents\github\mb_market_data\output\polling_resilience_v1
```

The numbered CMD files must be directly inside that folder. With sea-green
active, run `01_prod_main_offline_verify.cmd` in prod-main. It uses the bundled
candidate source, synthetic journals, fake quotes, and separately launched
synthetic processes. It opens no production journal and makes no API request
or password prompt. Existing production pollers and monitor continue.

The checks cover temporary and permanent writer contention, rollback before
retry, immutable sample identity/timestamps, conflicting records, parallel
Uni/Focus writes, retained acquired evidence, visible missing slots, repeated
replay, inspector exit and hang detection, and stopping only an owned monitor
child. Native Windows checks inspect Python processes and the supplied Tk
runtime without sounding the desktop alarm.

## Installation after the close

Finish the normal daily production audit/replay first, salvage that day's saved
evidence if needed, and stop prod-health with Q. After 16:00 ET, run
`02_prod_main_install_after_close.cmd`. It refuses an active polling window,
an existing poller/monitor process, unknown process inspection, changed source,
an unexpected Git commit, a dirty working tree, or missing Windows verification.

The installer fast-forwards only the reviewed baseline commit using the bundled
Git objects, preserves the previous standalone tool folders, and updates their
reviewed-source fingerprints. It does not migrate or open daily journals. The
saved rollback bundle and backup folders are recorded in its receipt. If a
later deployment step fails, retain the receipt; do not launch production until
the source/tool consistency checks pass.

No GitHub push, pull request, service installation, API restart, credential
change, or live production failure injection is part of this increment.

## Write recovery policy

Immutable run/revision records, acquisitions and empty-membership skips use a
fresh `BEGIN IMMEDIATE` transaction for each attempt. Failed attempts roll back
and close before waiting and trying again. Only numeric `SQLITE_BUSY` and its
extended codes are retried. `SQLITE_LOCKED`, I/O/corruption errors, validation
errors, and conflicting immutable records are surfaced immediately.

The default contention budget is five seconds with 250-ms SQLite waits and
at most 20 attempts. Backoff starts at 50 ms and is capped at 250 ms. Deadline
decisions use the monotonic clock. This limits retries; it does not impose a
hard real-time bound on OS scheduling, filesystem I/O or a successful commit.

The API response is acquired once and flushed to raw evidence before its write.
Retries reuse that response, slot ID, membership revision, dispatch/completion
timestamps and fingerprint. No API request or new observation is fabricated.
A persistent failure still stops the affected poller; a new process alone is
never evidence of recovery.

`journal_write_diagnostics.jsonl` in each evidence run records retry/failure/
confirmed commit events, operation, phase, database, writer PID, SQLite version
and numeric error, run/channel/slot/acquisition IDs, counts and elapsed time.
It cannot identify the owner of another connection's writer lock.

## Monitoring and display

The updated `02_prod_health_monitor.cmd` starts a supervisor and read-only
inspector child. The supervisor owns the existing persistent desktop alarm and
checks for a new completed inspection independently of SQLite/CIM operations.
Inspector exit alerts promptly; no completed check for 30 seconds alerts even
when its PID still exists. A stale report from another PID, generation or
journal cannot satisfy the watchdog. Q or the 05 stop launcher stops only this
monitor pair. A hung inspector may be terminated during explicit shutdown;
production pollers are never terminated by this tool.

The supervisor itself must remain running. OS-level supervision, unattended
poller restarts and encrypted-password handling remain later work.

The dashboard reads `output/polling_health/SESSION_DATE` by default, or an
explicit `--polling-health-directory`. Its health panel refreshes every five
seconds while replay is paused. Operational wall-clock information is labelled
separately from replay time. Wrong-journal/session and malformed reports are
refused; stale or stopped reports show last-known values without claiming
current channel health. The panel never starts a poller, changes membership,
writes a journal, or advances replay.

## Acceptance status

The previous standalone health tools passed 47 Windows checks and the operator
confirmed the persistent window, repeated tones and quiet recovery notice.
The previous synthetic failure/restart also preserved 17 Focus gaps while Uni
continued. This increment requires its own Windows verification. The original
October 6 lock owner/cause and an actual production API restart remain unproven.
