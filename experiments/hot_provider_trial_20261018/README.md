# Isolated native market-data integration and provider timing trial

This review moves the Windows-accepted `sampling_acquisition.py` into its native
`mb_market_data` package. At the initial integration commit d40a884, the module was unchanged from the accepted Coordinator
proof: SHA-256 `b2007cb3a3741afadc64e19b6c3885c55d3cbdd36c82704d2cc63771a9499611`.
It has no Coordinator import. The exact production baseline is
`a082f544112643b4e5f459d15c67f60d0a1ef83d`. Existing Uni/Focus cadence and launchers
are unchanged. This branch/bundle does not install source into production.

## Windows use

Extract the uniquely named ZIP under `mb_market_data\output`. Keep the complete
contained folder. With the existing sea-green environment active, run the
absolute path to `01_verify_hot_integration.cmd` from any working directory.
It verifies payload hashes, uses this folder's native source, runs 54 tests and
then two short actual-clock synthetic phases. No password, network or API call
is needed. Console output announces the scheduled wait and each outcome.

After it passes, run the absolute path to `02_run_schwab_hot_trial.cmd` as a
separate command. It asks for the existing encrypted-config password once.
Nothing should follow a password-prompting command in a pasted block.
Do not paste the password or OAuth redirect URL into chat.

The authenticated pilot requests **AAPL, MSFT and SPY** with one client:

| Phase | Interval | Duration | Planned quote-client calls |
| --- | --- | --- | --- |
| 1 | 2 seconds | 60 seconds | 30 |
| 2 | 4 seconds | 60 seconds | 15 |

Each call fits one batch. The hard quote-client call budget is 45; SDK token
refresh HTTP calls are outside that count. Request timeout is explicitly three
seconds through the existing secure-client interface. Credential preparation is
timed separately from quotes and excludes password typing. An access token can
refresh through the existing client; interactive OAuth is refused. If required,
run `mb-schwab-auth` separately and retry with a fresh trial directory.

The trial creates fresh schema-v2 journals and acquisition sidecars beneath
this bundle's `runs` folder. All three symbols belong to Uni/Focus/Hot in each
isolated fixture, with only the Hot channel acquiring. It accepts no production
journal or existing run directory. No environment install, production launch,
membership publication into the daily journal or live halt integration occurs.
The Schwab client uses the existing token database and may refresh its access
token in the normal way. No credential schema or OAuth settings are changed.

Ctrl+C ends the trial and preserves evidence; allow an in-flight request or
SQLite operation to return. A request timeout is not an absolute process-runtime
guarantee. Do not close the window to interrupt a run if Ctrl+C is available.
A lost/unknown dispatched request remains a visible gap and is never repeated
by this driver. Rerunning the launcher starts a new trial.

## Interpretation

`runs/.../summary.txt` and `manifest.json` report both phases; each phase also has
`report.json`, journal and sidecar. They retain round-trip request timings,
collector/journal/active-cycle timings, dispatch lateness, per-slot outcomes,
HTTP status/exception type, observation statuses, full planned-window coverage,
audit findings, repeated acquisition-aware replay hashes and code hashes.
Timing summaries use nearest-rank p95. Collector/journal timings are nested in
active-cycle wall time; do not add nested costs to the cycle total.
Decoded successful quote payloads and normalized rows are retained in the
accepted durable sidecar. HTTP error bodies, request headers, credentials and
arbitrary SDK exception messages are excluded. This is not a wire capture.

A slow request can pass the next scheduled slot. There is no catch-up burst:
the missing slot stays visible. Quote request errors have explicit journal rows;
therefore complete coverage and all-successful quotes are separate checks.
The full-window audit counts remaining slots as missing after an interrupted
run; the audit at stop is also recorded. The last slot is before the exclusive
phase end, so normal completion can precede that boundary slightly.

Weekend/after-hours results measure local work and provider transport for this
small roster. Stale quote/trade times then are expected; they are not a poller
heartbeat or evidence of a missed request. The exchange calendar is not checked.
Even clean results do not establish capacity for large Hot rosters, simultaneous
Uni/Focus/Hot load, live Nasdaq feed delay or trading-hours behavior. Those are
separate next measurements. Two seconds remains a target, not a production
default. Outside-Uni admission remains the accepted Coordinator capability;
real catalog/broker resolution and production transport/authentication decisions
remain pending.

The source manifest detects accidental payload changes; it is not a signature.
Run reports are fresh, machine-specific evidence and repeated replay of the
same journal must agree; hashes of separate actual-clock runs will differ.


## MasterBot acceptance — October 17 evening / October 18 ET

The supplied complete CMD transcript records all **54 tests PASS in 3.390s**,
followed by the actual-clock synthetic proof PASS (three acquisitions, nine
quote rows). The authenticated Schwab pilot then completed **45/45 acquisitions,
135/135 quote observations, zero missing slots, no reported errors or
interruption**, and returned to its final CMD prompt. Client preparation took
1.099s. The fixed source guard and production baseline were printed by both
launchers. Accepted implementation: `d40a884`.

| Phase | Acquisitions | Request mean / p95 / max | Active-cycle mean / p95 / max |
| --- | --- | --- | --- |
| 2 seconds | 30/30 | 227 / 281 / 345 ms | 462 / 657 / 743 ms |
| 4 seconds | 15/15 | 222 / 378 / 378 ms | 353 / 553 / 553 ms |

Journal mean was about 5.7ms in both phases. Maximum dispatch lateness was
200ms / 109ms respectively. The worst observed complete 2s cycle used about
37% of its interval. This supports expanding the measurement; it does not
establish a production capacity bound. Request latency was similar across
these two small samples. The complete-cycle overhead includes evidence
persistence and consistency work as well as the request and journal commit;
its individual costs have not yet been profiled. The shorter phase-size trial
cannot establish long-session scaling.

The result was after hours (00:57–00:59 ET on October 18). Detailed reports
remain on MasterBot at the path recorded in
`validation/masterbot_acceptance_20261018.json`. This acceptance is based on the
supplied console evidence; those databases/reports were not independently
retrieved, and host replay hashes were not printed in the transcript. The
runner's full-coverage result incorporates its audit/repeated-replay checks.
The accepted ZIP remains unchanged. This documentation-only acceptance update
requires no rerun. Next measure a representative larger Hot roster and longer
run, then combined channel load and trading-hours behavior before production
activation.


The current review branch includes the subsequent [larger-roster trial](../hot_scale_trial_20261018/README.md), with derived normalization reuse and shared historical-read connections. The initial accepted ZIP and recorded hashes remain historical evidence; use the new scale bundle for this follow-up.
