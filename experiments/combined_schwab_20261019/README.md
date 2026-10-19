# Bounded combined Schwab review

This optional experiment measures the native incremental Uni/Focus/Hot collectors
with actual Schwab responses while a separate Coordinator process publishes
simulated membership requests. It writes fresh marked experimental journals.
It installs no package, changes no production source, and never opens the
production quote journal. Normal production Uni/Focus continue independently.

## Fixed scope

- Five minutes at Uni :00/:30, Focus :05/:20/:35/:50 and Hot every two seconds.
- Same-day opening r0 and Focus r1 proposal JSON are read-only roster inputs.
  Accept 401..600 opening Uni symbols, exactly 40 Focus symbols, no opening Hot.
  Both Uni rosters must match. Raw input SHA-256 values are retained in the plan.
- Initial experimental Hot contains the same 40 Focus symbols. A simulated
  manual request admits MSFT from outside Uni; a separate simulated halt source
  sustains it through manual withdrawal. An algorithm Focus request, temporary
  privileged Uni cap, automatic expiry/reentry and delayed demotion are exercised.
  MSFT remains in experimental Uni after demotion. No real Nasdaq request occurs.
- Hard durable quote-client budgets: Uni 20, Focus 20, Hot 150; total 190 calls.
  Uni has two batches per acquisition; Focus/Hot one. Normal schedule calls
  average 38/minute for this experiment. This is an experiment budget, not a
  claim about provider limits or other clients' traffic. SDK authentication and
  token-refresh HTTP calls are outside this counter.
- Request timeout three seconds; unchanged 750ms dispatch-lateness gates;
  no catch-up requests for missed slots. Misses/unavailable outcomes fail the
  trial rather than being hidden or filled by later requests.
- Review policy: two-second grace, four-second minimum residence, 0.5s publication
  lead. These are explicit experimental settings, not adopted production policy.

## Operator sequence

Extract the unique ZIP into `mb_market_data/output`, retaining its top-level
folder. Use an idle sea-green CMD and absolute `call` paths from any directory.
Keep production health visible; stop the experiment first if production suffers.

1. Run `12_verify_combined_schwab.cmd`. It disables network access, runs focused
   tests and a 60-second four-process synthetic proof, then saves a gate tied to
   exact source/report hashes. No password or API request. This new package has
   new live transport controls and requires its own verification.
2. After verification passes, call `13_run_combined_schwab.cmd` with **two quoted
   arguments**, the complete opening r0 proposal path and the complete Focus r1
   proposal path. This command prompts for the encrypted-config password once;
   put no following commands in the pasted block. The package resolves existing
   `.ecfg` via MB_SCHWAB_ECFG, MB_VAULT, then the user's MBV directory. There is no
   plaintext or current-directory credential fallback.
3. Leave normal production processes running. Expect worker setup and minute
   alignment (roughly 45..105 seconds), then five minutes of polling and final
   audit/replays. Progress is printed every ten seconds and at each scenario.
4. `14_stop_combined_schwab.cmd` from another idle CMD, or Ctrl+C in this runner,
   stops only this experiment. Stop requests do not touch production processes.
5. Send the summary and a fresh production health status. Keep original quote
   evidence local; public source control contains generic code/synthetic tests.

Live launch is guarded to weekdays, 09:30 <= ET < 15:50. This is a time-of-day
guard, not an exchange holiday calendar. Do not use it as a market-calendar check.
Client preparation rejects browser reauthorization and checks refresh coverage
through a conservative ten-minute horizon plus the existing credential margin.
The parent preflights once, then three independent workers prepare their clients.
Their secret is sent over stdin pipes, never command-line arguments, environment
variables or saved files. Each client may refresh the shared token database;
this is the only intended shared write with existing production clients.

HTTP 401/403 or 429 requests a global experimental stop. Existing in-flight
requests may finish; a three-second request timeout is not a hard process-runtime
bound. Request-start reservations are durable; unknown interrupted outcomes
remain explicit gaps and are never automatically reissued. Only owned Popen
handles may be terminated during cleanup. The runner never searches production
PIDs or window titles.

## Evidence and limitations

PASS requires complete configured coverage (10 Uni, 20 Focus, 150 Hot), quote
outcomes, correct hierarchy lifecycle, stable native incremental verification,
no unresolved dispatch, audit PASS, identical repeated journal replay and matching
request/publication restart reconstruction. Cycle, verification and actual
provider-request timing are separate inclusive metrics; do not add nested costs.

The offline proof deliberately delays one synthetic Hot response by 1.1 seconds
to exercise in-flight publication. **No response delay is injected in live mode.**
Natural live overlap is observational, not a prerequisite for its capacity PASS.
Synthetic requests do not establish live Nasdaq detection latency. A five-minute
fresh-history result does not establish full-session history/growth capacity,
outside-Uni production catalog validation, or authorization to enable routine Hot
polling. Production main and existing launchers remain unchanged.
