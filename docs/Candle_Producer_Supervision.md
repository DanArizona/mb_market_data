# Candle-producer supervision and the first live candle trial

## Alert priority

Uni, Focus, and future Hot quote polling need urgent attention on interruption:
the observations missed while those pollers are stopped cannot currently be
reconstructed reliably. Their production alarms keep their existing behavior.

Candle input is normally recoverable. Candle failures get a quiet, persistent
visible warning, console status and durable incident evidence. They do not open
an alarm window, flash, beep or request a quote-poller restart. Future halt and
indicator sources should use this recoverability distinction too; this change
implements candles only.

Every candle observation keeps its actual request dispatch and response receipt
times. A backfilled candle becomes known at its recovery receipt, including when
its market interval is much earlier. Seek and replay retain that distinction.

## Components and ownership

- The supervisor launches and retains the exact `Popen` child handle. Credentials
  are entered in its console by the producer; the supervisor never reads them.
- The child acquires one symbol, saves raw responses and commits immutable,
  completely published receipts. A flushed raw body precedes its receipt. A
  temporary JSON file becomes a new final filename atomically and cannot replace
  existing evidence. NTFS hard-link publication is checked by the offline tests.
- A separate watchdog checks the supervisor's heartbeat. The Overlay independently
  checks both heartbeat ages using wall time, even while viewing is paused or
  seeking backward.
- The Overlay reads candle evidence and health, and initializes its own empty
  synthetic quote fixture. It does not open a production quote journal. Stop
  server closes only this viewer; acquisition and supervision continue.

Health is based on validated, newly committed responses. Identical candle values
can be healthy progress. Old provider candle timestamps and process presence do
not determine input health. Acquisition errors are shown separately from stale
supervisor/watchdog heartbeats. Status writes retry bounded Windows sharing
denials while retaining the old status; failed incident publication is retried
on later inspections without discarding the incident.

Waiting for credentials is visible and does not trigger an acquisition-overdue
warning. A running child is overdue after request interval + 10-second HTTP
timeout + 10-second grace without a successful acquisition. Supervisor and
watchdog status older than 10 seconds is a quiet warning. Both processes continue
monitoring after producer completion or failure until the session is closed.

## Recovery

Recovery is manual. Restart is refused while the owned child is still running.
A restart creates a new numbered run, preserving previous raw responses,
receipts and incident events. The viewer can follow the several runs belonging
to this one supervised session, using each receipt's actual acquisition time and
run identity. This does not merge unrelated old captures or change the saved SPY
capture used for earlier acceptance.

Launching a replacement does not clear an interruption. A newly validated
successful response at or after detection restores input flow. The recovery
event records its exact acquisition time. This does not assert complete backfill:
the event and status retain `NEEDS_REVIEW` for missing intervals and later
corrections. The read-only audit verifies hashes, timestamps and reconstruction,
and reports that remaining review explicitly.

An OS-held producer lock blocks duplicate acquisition within the chosen output
root for the same symbol and session, including if its supervisor exits. Lock
files can remain after completion; OS lock ownership releases on process exit.
The supervisor does not attach to or terminate an unowned process. On requested
stop, it allows 12 seconds for its child to finish an in-flight bounded request.
If cleanup must be forced, only the retained child handle is targeted and the
stop receipt records it. Forced cleanup cannot promise a final producer summary.

## Operator sequence

1. Install the packet into the existing isolated candle worktree, with its viewer
   stopped through Stop server. Run the packet's offline verification on Windows.
2. Run the synthetic demo in a dedicated candle-producer CMD. It starts with
   synthetic API errors and leaves a quiet warning after the child exits.
3. Open the viewer in a separate overlay-candles CMD using the printed exact
   session path. Inspect the warning and restart through the packet command.
   Expect fresh responses, recovery recorded and backfill review still pending.
4. Pause, seek and return to live. Current health remains current; a late recovered
   observation does not appear before its acquisition time. Stop server should
   release the viewer while the producer/supervisor remains independent.
5. Close the candle session with its dedicated command and run the read-only audit.

Ctrl+C viewer termination is deferred and is not an acceptance prerequisite.
The installer preserves whichever reviewed console revision is already present.

## Bounded real API trial

After the offline Windows checks and visual drill pass, the packet includes a
launcher for one SPY capture: 12 minutes, five-minute candles, requests every
15 seconds, 10-second HTTP timeout. It skips missed request slots and never
bursts requests to catch up. It uses the existing encrypted configuration and
client, with a password entered locally in the producer console.

Run only on a known open market day during 09:30–16:00 ET, with at least 13
minutes left before close. The producer rechecks the window after authentication.
Weekend, wrong-date, out-of-window and insufficient-time launches are refused.
The calendar does not identify exchange holidays; the operator explicitly
confirms an open session with the reviewed launcher. API 401/403 or three
consecutive errors stop the bounded child; supervision keeps the warning visible.

The first trial is candle-only with actual receipt time and an explicitly labelled
empty quote/membership fixture. Observe at least two five-minute boundaries,
pending/closed behavior, Pause/Seek/Return to Live, health and independent stops.
Preserve the complete session and audit it afterward. Production-journal
attachment is a subsequent acceptance step. Multi-symbol scheduling, automatic
restart, halt acquisition and indicator acquisition remain separate work.

This bounded producer and its manual restart acquire the same day's regular
session only. Historical backfill after close or on a later date is a separate
ingestion step; this increment does not automate that retrieval. The first
supervised trial proves interruption detection and recovery within its window.
