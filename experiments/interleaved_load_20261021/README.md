# Interleaved bounded-late overrun review

This separate experiment uses live Schwab quotes and controlled membership
requests in fresh experimental journals. Normal production Uni/Focus and their
monitor continue with their current schedules. No installation or production
journal access occurs. The original interleaved live FAILED result and accepted earlier qualifications are preserved.
This v2 package changes only the experimental slot selector; it does not install
a production scheduler.

## Schedule and bounds

| Channel | Seconds within each minute | Acquisitions in 30 minutes | Quote-call ceiling |
| --- | --- | ---: | ---: |
| Uni | 00, 30 | 60 | 120 (two batches) |
| Focus | 10, 20, 40, 50 | 120 | 120 |
| Hot | 02,04,06,08; 12,14,16,18; 22,24,26,28; 32,34,36,38; 42,44,46,48; 52,54,56,58 | 720 | 720 |

Total quote-call ceiling: **960**. Authentication/token-refresh HTTP is separate.
These are experimental budgets, not provider limits. Timeout remains 3s;
first-dispatch lateness remains 750ms. An uninterrupted worker retains its next
planned slot after the previous cycle, acquiring it only while all existing
dispatch gates remain satisfied. Past-budget slots receive durable native
missed-slot events and remain failed audit coverage; no expired backlog quote
requests occur. Starting/restarting still selects a future slot. No automatic
live restart occurs.

Read-only inputs are the same-day opening r0 (401..600 Uni) and Focus r1
(exactly 40 matching-Uni symbols, opening Hot empty). Their hashes are retained.
The initial 40 Hot symbols are the opening Focus roster. A controlled outside-Uni
MSFT admission can temporarily make Hot 41; an independent algorithmic Focus
request, source withdrawal, temporary cap, cap expiry and delayed demotion are
also exercised. These are simulated requests with real quote responses.
There is no live Nasdaq acquisition or real-halt latency claim in this trial.

All three quote workers use the unchanged native incremental collector and the
pinned Windows-accepted Coordinator source 0417b345e2725f4e1e184d3b3202bedb846dbfca.
The old production health monitor's cadence is deliberately not changed.
This capacity profile checks Coordinator requests/timers once per second, with
a 0.5s publication lead; every cycle retains full native validation. These are
review settings, not production defaults. Detection of a newly received request
can therefore take up to that one-second polling interval, in addition to
publication and the next eligible quote slot. The offline in-flight publication
fixture retains one 1.5s synthetic Hot response. An additional Hot :18 response
waits 3.818881s and then injects 0.35s of local post-response work, exercising
the next :22 slot. A 1.461032s Focus :20 response reproduces the overlapping
slow-call pair and visible combined-gap warning. Live mode injects none of
these delays. Native scheduled-slot
membership binding is preserved even when dispatch is slightly late.

## Operator sequence

1. Extract the unique ZIP into `mb_market_data/output`, preserving its enclosing
   folder. Use an idle sea-green CMD and absolute `call` paths.
2. Run **23_verify_interleaved_load.cmd**. It disables network, runs the changed
   boundary/qualification tests, then a **60-second actual-elapsed** four-process
   synthetic proof. Expected coverage: Uni 2/2, Focus 4/4, Hot 24/24; 32 batch
   calls. It checks audit, repeated replay, lifecycle and combined symbol
   coverage. Expected overall trial verdict is **PASS_WITH_WARNINGS**: deliberate
   long response gaps stay visible. A separate changed-path qualification PASS
   requires complete coverage, all quotes, audit/replays and the late :22
   acquisition; it never rewrites the warned trial verdict. This qualifies
   the changed package; it does not repeat the accepted
   full-history or old live qualifications. Expect about 80 seconds plus tests
   and cleanup. Send its summary and confirm actual prompt return.
3. Obtain a fresh production health report before the separate live command.
4. During an independently confirmed regular market session, invoke
   **24_run_interleaved_load.cmd OPENING_R0 FOCUS_R1 --market-open-confirmed**.
   Quote both proposal paths. Launch guard: weekdays, 09:30 <= ET < 15:25;
   the full acquisition window must end by 16:00 ET. The weekday guard is not
   an exchange holiday check. Client preflight requires a 35-minute refresh
   horizon plus the existing credential margin. One password prompt follows.
   Never put a password in command arguments, environment variables or files.
5. Allow worker preparation/minute alignment (roughly 45..105 seconds), then
   30 minutes of polling and final read-only assessment. Progress prints every
   ten seconds. Do not start another provider experiment concurrently.
6. Ctrl+C in the runner, or **25_stop_interleaved_load.cmd** from another idle
   CMD, stops only this package's owned experiment. If production suffers,
   stop this experiment first. It never searches or kills production PIDs.
7. Retain the completed folder, summary/report, journal, raw acquisitions and
   call/status receipts. Send the summary and a fresh production health report.

The shared SDK token database can refresh. HTTP 401/403/429 stops the experimental
channels. Existing in-flight calls may finish; SDK timeout is not a hard process
runtime guarantee. Interrupted unknown outcomes remain explicit and are not
reissued. Each worker receives its secret through a private stdin pipe.

## What the report establishes

- **Per-channel collection:** exact saved grids, missing slots, request budgets,
  native audit, repeated journal replay, immutable lifecycle/revision binding
  and request/publication reconstruction. Invalid outcomes remain explicit
  availability warnings; missing acquisitions, transport errors or bad evidence
  fail independently.
- **Combined cadence:** the 40 opening-Focus symbols stay in Hot throughout.
  Their expected scheduled opportunities are every 2s across all channels,
  every 10s across Uni/Focus, 10/20s in Focus only and 2/4s in Hot only. Other
  dynamic admission is independently audited, not claimed as an all-window
  stable cohort. A missing higher-channel slot is never fabricated by Hot.
- **Actual gaps:** per-symbol response receipts and post-write commit-observer
  receipts, with channel provenance preserved. Missing/invalid quotes cannot
  masquerade as complete quote coverage. Gaps over the explicit 4s review
  threshold produce a cadence warning, never a clean PASS. This threshold is
  an experimental review setting, not an adopted production default. A receipt
  does not guarantee a fresh provider quote/trade timestamp or an executed trade.
- **Overlap:** cross-channel overlapping SDK quote-call intervals, with examples.
  This is not a wire-level audit of authentication or other clients' traffic.
- **Growth and resources:** cycle/request/verification timings by thirds,
  worker CPU and memory samples, and fresh-journal startup reconstruction time.
  Each channel also lists scheduler overruns, acquisitions admitted after an
  overrun, and expired-slot misses.
  Memory availability is explicit. Fresh startup is not the full-day cold
  reconstruction or restart cost, and no live restart drill is performed here.

PASS_WITH_WARNINGS may reflect explicit invalid-symbol availability or observed
combined-gap warnings. Review the separate collection, transport, availability
and cadence sections. An offline PASS is not the live 30-minute capacity result.
No full-day capacity, production cadence adoption or production Hot enablement
follows automatically from this experiment.

The retained original live failure establishes the skip mechanism, not the
underlying cause of a slow SDK call. Bounded late selection cannot eliminate
long actual response gaps or guarantee that a provider responds within a slot.
Coordinator history-cost growth remains a separate profiling/adoption concern;
this package preserves its pinned source and validation checks.
