# Supervised candle capture and controlled live Overlay trial

Preparation dated 2026-10-11 UTC. This is the next implementation and acceptance
contract, not authorization to attach the current prototype to production.

## Current position

The 2026-10-09 SPY evidence has 49 immutable receipts. Its response-based
projection, single-candle display, playback counts, and 57-to-58 closed-candle
transition have passed the operator's isolated review. Browser Stop server
passed. Native Ctrl+C did not pass and is the immediate correction to verify;
Q is an additional stop route. The separate production monitor status-write
correction has already been installed and its synthetic alarm checked.

The current candle viewer still excludes production opening journals. The
existing live-candle capture is a bounded evidence probe, not yet a supervised
continuous producer. This correction changes the viewer lifecycle only.

## Ownership and evidence boundaries

| Component | Owns | Reads | Writes |
| --- | --- | --- | --- |
| Membership coordinator | Authoritative Uni / Focus / Hot revisions | Automatic proposals and manual membership requests | Atomic nested membership revisions |
| Production quote pollers | Scheduled quote acquisition | Published membership per slot | The existing daily quote journal and their evidence |
| Production health monitor and watchdog | Quote-poller and monitor health | Committed slots, process and monitor status | Health status, incidents and operator alerts |
| Separate candle producer | One symbol's bounded price-history acquisition | API responses | A new candle evidence directory per run |
| Candle supervisor | Its own producer's lifecycle and progress | Owned child process, committed receipts and status | Separate supervisor status, incidents and receipts |
| Live Overlay | Presentation and inspection time | Daily journal read-only, candle receipts, health | Viewer-local stop/acceptance receipts only |

Manual membership requests go to the coordinator for validation and publication.
Neither the Overlay nor a candle supervisor changes membership or restarts a
production quote poller.

## Required producer work before live attachment

1. Move the bounded capture into the isolated reviewed source. Retain raw
   responses, hashes, acquisition/receipt timestamps, errors and run identity.
2. Publish a receipt only after its raw response is durable. Write receipt JSON
   completely to a temporary file, flush it, then publish the new final name
   without replacing earlier evidence. The existing capture writes receipt JSON
   directly to its final filename; a live reader could encounter a partial file.
   Orphan raw responses remain evidence but are not committed input.
3. Track the exact child launched by the supervisor. Distinguish waiting for
   credentials, starting, running, overdue, failed, stopped and window ended.
   Never inspect a password or consume the child console's credential input.
4. Define progress as a newly validated committed receipt. Repeated candle values
   may be a successful API response. Process presence or old provider candle
   timestamps alone do not prove success or failure. Report API/data errors
   separately from a missing producer heartbeat.
5. Keep supervising after child exit. Show CANDLE / symbol, last committed receipt,
   failure reason and the operator response in a persistent alarm. Restarting
   must create a new capture run and preserve earlier evidence. Initial recovery
   remains manual; no automatic API restart is introduced.
6. Publish status with bounded handling of transient Windows file-sharing errors.
   A failed status write must be visible rather than silently ending supervision
   or declaring recovery. Detect a stale supervisor from an independent owner;
   a producer supervisor must not be the only observer of its own heartbeat.
7. Stop responsively between requests, with a finite timeout for an in-flight
   request. Cleanly close the API client and finalize a run receipt. Any forced
   cleanup must target only the exact owned child. A viewer stop must leave the
   producer and both production quote pollers running.

The reader already ignores raw files lacking final receipts, preserves validated
state on errors, and projects only evidence received by the inspection cutoff.
Those properties must remain intact while live files are appended.

## Offline acceptance before API use

Use fake responses and isolated directories to demonstrate:

- partial publication, orphan raw files and immutable earlier receipts;
- unchanged candles with new successful receipts remaining healthy;
- API errors, a hung owned child and abrupt exit remaining visible;
- supervision continuing after producer exit, and separate supervisor failure;
- Windows status-write contention, repeat alarms and quiet recovery notices;
- no recovery declaration until a replacement run commits a new response;
- capture/viewer stop routes, released ports, preserved evidence, no duplicate
  child and no access to a production journal during the synthetic drill;
- two fresh-process reconstructions agreeing at receipt cutoffs.

## First controlled live trial

After native console acceptance and the supervised producer drill pass, prepare
a reviewed launcher for one SPY capture, 12 minutes, one request every 15 seconds,
10-second request timeout, with no expansion to a watchlist. Run it only during
an explicitly checked regular market session and available credential window.
The first attachment uses a separate candle-only fixture and the actual receipt
clock. Label the source and the absence of quote/membership evidence clearly.

Observe at least two five-minute boundaries, pending/closed receipt behavior,
Pause/Seek/Return to Live, source-health status and independent stop controls.
Afterward retain the complete capture, audit hashes and reconstruct the same
cutoffs in a fresh process. Run the ordinary production quote-journal closeout
separately.

Only after that trial passes should a separate reviewed step enable read-only
attachment to the production daily journal. Confirm session identity, preserve
quote acquisition/availability and candle receipt time separately, and verify
that membership shading follows published revisions. Production readers must
not write, checkpoint, migrate, recover or restart the journal or its pollers.

## Later scope

Multi-symbol candle scheduling, continuity across producer runs, supervised
automatic restart, wider real-time Overlay rollout and manual membership-request
UI remain separate work. The coordinator retains membership ownership. The
earlier SQLite lock's precise external cause is still unproven; bounded retries
and diagnostics do not establish that cause.
