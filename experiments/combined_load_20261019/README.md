# Isolated combined synthetic load review

This review starts four separate processes: the accepted Coordinator publication
service and native guarded incremental Uni, Focus and Hot collectors. It uses
generated instruments and quote responses. It does not read production journals,
credentials, token databases, watchlists or halt feeds. Network connections are
disabled in the runner and all workers. No installation or source patch occurs.

Run in the `sea-green` CMD environment. Extract the uniquely named ZIP into
the `output` folder of your market-data checkout, preserving its top-level
folder. The absolute `call` commands work from any current directory.

1. `09_verify_combined.cmd`: focused tests, then a 60-second actual-clock review.
   Allow about 90 seconds including startup and audit/replay. A passing report
   creates a source/report-bound verification receipt in this package.
2. After normal production Uni/Focus and its health monitor show RUNNING,
   `10_run_combined.cmd`: five-minute synthetic combined load plus about 18 seconds
   of setup and final checks. It requires the unchanged verification receipt.
3. `11_stop_combined.cmd`: run from a separate idle CMD to stop this package's
   active experiment. Ctrl+C in the experiment console also requests cleanup.
   Only child handles created by this runner can be terminated; production PIDs
   and windows are never inspected or stopped. A stopped run is not a PASS.

The 60-second verification expects Uni 2, Focus 4, Hot 30 scheduled outcomes.
The five-minute review expects Uni 10, Focus 20, Hot 150. Uni uses :00/:30,
Focus :05/:20/:35/:50 and Hot every two seconds. The 750ms dispatch deadline,
slot-time revision binding, durable result recovery and no-catch-up rules are
unchanged. All three experimental collectors use native incremental verification;
normal production Uni/Focus continue using their existing launchers.

The immutable synthetic opening contains 554 Uni and 40 Focus symbols. An initial
source-owned request supplies 40 Hot symbols before the opening slot. The scripted
sequence adds algorithmic Focus demand, admits an outside-Uni instrument into Hot,
adds independent synthetic halt-source demand, withdraws manual demand, imposes a
temporary Uni cap, lets it expire, and withdraws the remaining higher-group demand.
The admitted instrument stays in Uni for the session. Final counts: 555/40/40.

One explicitly labeled Hot response takes 1.1 seconds to create a controlled
in-flight publication overlap. Other synthetic batches wait 180ms Uni, 200ms Focus,
250ms Hot. This is a healthy-coverage test: no missing slots are injected and none
are accepted for PASS. No network/provider latency inference is made from these
chosen delays. Fault/recovery stress and full-session prefill are separate work.

All polling follows actual elapsed wall-clock speed. A fixed offset places the
experiment in a labeled future synthetic session to avoid midnight dependence.
No logical stepping is used during polling. Real SQLite publication timestamps
are retained and mapped with that offset only when checking in-flight overlap.

Progress prints every ten seconds and on each scenario input. `runs/combined/...`
contains each worker's console, status and synthetic batch receipts, request and
publication ledgers, quote journal, `report.json`, `summary.txt`, and `result.json`.
PASS requires complete per-channel slot coverage, correct final membership and
admission/cap/reentry/demotion history, independent-source retention, a publication
during the pending Hot response without rebinding it, no steady-state full scans,
full audit, two identical acquisition-aware journal replays, and stable request
and publication reconstruction on reopen.

Keep the regular production health monitor visible. Both workloads share CPU,
memory and disk. If production health degrades, stop the experiment first.
The package does not promise zero host-resource contention. No full-day or active
market capacity, live Nasdaq detection, production setting adoption or merge to
main is established by this synthetic review.

The dependency under `coordinator_src` is copied unchanged from
`DanArizona/mb_watchlist_coordinator` commit
`0417b345e2725f4e1e184d3b3202bedb846dbfca`, tree
`2b1fae92882b259c1490aeed2ac17e59b4b2f148`. `coordinator_pin.json` records every
Python source hash. It is a bundle-local dependency, not an installed package or
new Coordinator implementation. A source checkout must stage those pinned files
under `coordinator_src/mb_watchlist_coordinator` before the integration review;
the delivered ZIP includes them. `source_manifest.json` pins both packages, the
runner, tests and launchers, and is generated when packaging. Private captured
payloads are not needed or included.
