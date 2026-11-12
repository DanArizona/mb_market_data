# Daily flow candidate — November 6, 2026

**Candidate for offline Windows verification. Not installed, not a production merge,
not a Hot activation.** The live entry requires an explicit `start --live`; `plan`
never requests credentials or contacts a provider.

## Daily behavior implemented

Start and unlock before 09:25 ET. One human `.ecfg` unlock is passed privately to
owned background processes; no password enters an argument, environment variable,
plan, status file, log, or saved credential file. The installed `mb_tools` secure
client factory and token store remain the authentication boundary. Python memory
clearing is best-effort, not a guarantee of physical RAM erasure. A full controller
restart needs another human unlock; it does not require each worker to prompt.

At 09:25 ET, current quote `totalVolume` ranks the existing r0 Uni descending,
with symbol ascending for ties. Focus remains **40**, not 50 or 100. All ranked
symbols and the top 100 are retained. Missing values are not zero. Missing
symbols are retried once within the 09:29 cutoff; timestamps and the attempt
number make later recovered values visible. The selection can be degraded with
fewer than 40 usable names. No candle acquisition is part of normal startup.
Completed batches are saved individually before further requests.

A successful selection creates and publishes the actual schema-v2 opening r1.
Uni is prepared before selection, Focus starts after publication, and both wait
for 09:30. They execute the existing native quote probe with explicit
`interleaved-v1`: Uni :00/:30, Focus :10/:20/:40/:50. The monitor uses exactly the
same window and profile. Hot seconds remain reserved; this package does **not**
start or replace the Nasdaq/manual Hot operating delivery.

The controller checks native journal progress independently of process presence.
A missed slot remains a gap, not a reason by itself to stop the session.
Per-symbol invalid results remain explicit. HTTP 401/403 pauses new calls;
429 causes bounded backoff. Fatal storage failures stop the affected probe
rather than claiming unsaved observations were recorded. Workers are independent.
There is no tight automatic process restart loop: restart a failed channel through
its owner, using the same unlock and the retained ranking.

At scheduled exchange close + 60 seconds, the controller starts the broad
symbol-directory/quote scan using the already-held unlock, irrespective of Uni
worker success. It uses the existing Universe filters, constructs the next r0,
and publishes it to the next session's journal. Current selection and post-close
preparation are separate phases; a missing Focus snapshot does not cancel Uni
or the evening task. A partial broad transport response is retained but is not
published as a smaller normal Universe. A completed build can be reconfirmed or
published without new API requests; incomplete existing output is never overwritten.

Exchange dates and closes use installed `exchange_calendars` XNYS data. Holidays,
weekends, early closes, and next-session dates are resolved together. This is a
scheduled calendar, not a live emergency-closure feed. The source snapshot must
still be obtained on its original Eastern date; later volume is never relabelled
as the previous day's snapshot.

## Offline Windows check — first action

Use the **existing `closeout-qa` conda environment**, which was previously cloned
from sea-green and has exchange-calendars installed. No installation or environment
update is included. The verifier requires the installed `mb_tools` import surface
on Windows, but substitutes every credential/client operation in the tests.

Extract this ZIP to a **new** directory, not over production or the voltest folder.
From an Anaconda/CMD prompt:

```cmd
conda activate closeout-qa
call "C:\Users\danla\dailyflow_qa\daily_flow_candidate_20261106\verify_offline.cmd"
```

Current scope: **137 tests (44 new daily-flow/runtime checks plus 93 regressions)**.
Network connections and interactive credential input are denied. Tests use
synthetic responses, temporary journals, a synthetic schedule clock, and real
owned child probe processes. They do not read a real vault, publish production
membership, use Schwab/Nasdaq, or install a scheduled task. The result is saved
under this extracted folder's `output`. A local non-Windows-only auth import stub
is explicitly reported; Windows may not silently substitute it.

The local process test confirms private pipe handoff, both real child probes,
interleaved registrations, four native acquisitions, and no canary password in
saved test files/logs. Controller-ordering tests substitute process launch and
use a logical clock; they are not a full elapsed trading session. Windows process
and SDK integration, token renewal, and an actual trading session remain separate
from the local results.

## Operator commands after Windows acceptance and explicit operating approval

These examples describe the interface; **do not run `start --live` merely to
verify the package**. The first operating date, r0 and data root must be reviewed.
All commands work independently of your current directory. No password goes in
any command. Use one environment consistently for the entire session.

```cmd
call "<candidate>\daily.cmd" plan --opening-proposal "<r0.json>" --market-data-root "<mb_market_data>" --ecfg "<secure_schwabdev.ecfg>"
```

The plan prints exact absolute paths and the calendar-derived times. A carried
or test roster needs the explicit `--allow-carried-roster` option; its original
metadata is preserved in the ranking history. No automatic stale-roster adoption.

```cmd
call "<candidate>\daily.cmd" start --live --opening-proposal "<r0.json>" --market-data-root "<mb_market_data>" --ecfg "<secure_schwabdev.ecfg>"
```

One password prompt, then a status viewer. Do not place another command below
this password-prompting command in a pasted block. The controller works in the
background. Closing the **viewer** or pressing Ctrl+C in it does not stop the
workers. Keep the machine awake until next-session preparation completes.

Public state directory: `<mb_market_data>\output\daily_session\YYYY-MM-DD`.

```cmd
call "<candidate>\daily.cmd" status --state-root "<state directory>"
call "<candidate>\daily.cmd" watch --state-root "<state directory>"
call "<candidate>\daily.cmd" stop --state-root "<state directory>"
call "<candidate>\daily.cmd" resume-collection --state-root "<state directory>"
call "<candidate>\daily.cmd" restart-focus --state-root "<state directory>"
call "<candidate>\daily.cmd" restart-uni --state-root "<state directory>"
call "<candidate>\daily.cmd" exit --state-root "<state directory>"
```

`stop` stops collection but **leaves post-close preparation armed**.
`resume-collection` re-enables collection in the original window; it never reranks.
`restart-*` requests a failed/stopped channel restart while collection is enabled;
healthy existing children are not duplicated. `exit` stops the whole daily owner
and **cancels pending preparation**. Requests are not confirmations: use status to
confirm exit and completed writes. Only exact owned child handles are terminated.
If the controller disappears, child checks stop orphaned activity after the owner
heartbeat becomes stale; an in-flight request can still complete.

A controller restart uses the same original parameters plus `--resume`. It
requires another human unlock, reads the original plan and saved selection, and
does not reset membership or acquire a different ranking after the cutoff. A
pre-cutoff failed partial selection can be preserved and retried explicitly;
a post-cutoff unavailable ranking stays unavailable while Uni/preparation remain
possible. Original journal gaps stay gaps. No past quote acquisition is fabricated.

## Daily history

- `selection/batch-*.json`: each completed quote batch with actual timestamps.
- `selection/snapshot.json`: input roster origin, data availability and raw quotes.
- `selection/ranking.csv`: all requested Uni symbols; unavailable values blank.
- `selection/top100.csv`: the ranked research shortlist.
- `selection/r1.json`: current-volume Focus proposal (when usable).
- `selection/publication.json`: actual r1 publication confirmation, separate from
  the immutable acquisition-stage `membership_published=false` snapshot.
- `status.json`, `STATUS.txt`, channel logs and native probe output.
- Next-day standard Universe artifacts and `daily_handoff.json`: READY only after
  r0 publication; successful build with failed publication remains pending.

The top 100 is a history list, not a larger Focus group. Uni retains its own
cadence; later comparison of 20/40/80 selections must consider differing sampling
frequencies. This package does not claim any ranking has proven trading value.

## Explicit limitations

No live provider calls, production install/merge, or Hot activation were performed
while creating this candidate. The accepted Hot/manual implementation and its
unresolved ordinary-demotion semantics are not changed here. Authentication
revocation or a system reboot can still require an attended unlock. The source
calendar dependency must be available in the chosen environment. The midnight
source-date boundary, scheduled close, partial-source visibility, and explicit
interleaved profile remain truthful constraints, not zero-gap requirements.

This is not a Windows service and does not add Task Scheduler or Pushover. It
cannot work while the computer is asleep/off. Simultaneous SDK token refresh
behavior and full-day operation are not established by synthetic tests.

## Provenance

Base: uploaded `voltest_candidate_20261105.zip`, previously Windows-tested in the
project. Existing collector, journal, health, filters, and snapshot code are reused.
Changes are listed in `changes.patch`; file hashes are in `SOURCE_INVENTORY.json`.
Local evidence, including initial test-harness failures and their corrections,
is retained under `local_evidence`; local results are not labelled Windows results.

Runtime process/calendar API references:
- Python: https://docs.python.org/3/library/subprocess.html
- exchange_calendars: https://github.com/gerrymanoim/exchange_calendars
