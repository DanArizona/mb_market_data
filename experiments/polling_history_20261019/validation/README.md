# Local synthetic history/recovery qualification

Implementation: `f391be1c4ee0ed19be0661f75e75f28f7bd0cb86`.
Production source and settings are outside this experiment.

The full proof passed 780 Uni, 1,560 Focus and 11,700 Hot regular-session
acquisitions, with no ordinary-session missing slots. It used 389 minutes of
logical native collector prefill and one final minute of actual elapsed
four-process scheduling. It did not run a six-and-a-half-hour elapsed session.
Membership during prefill was fixed at 554/40/40. The final minute exercised
outside-Uni admission, independent source withdrawal, a cap, expiry reentry and
delayed demotion.

The final minute committed 2/4/30 acquisitions with no full reconstruction in
warm polling. Hot cycle mean/p95/max: 302.835/423.687/468.558 ms. Cold startup
was separate: Uni 77.968 s, Focus 12.694 s and Hot 105.475 s. Cold workers used
substantial memory; these are operational costs to qualify on Windows, not a
portable startup or memory guarantee.

Four abrupt process-exit boundaries were tested against the retained Hot
history. Saved assembly and committed acquisition were recovered without a
second quote request. Unsaved dispatch/response became two explicit gaps after
the regular-session interval. Recovery clocks were logical. No rapid live
restart or gap-free wall-clock recovery claim is made.

The independent audit passed and two full acquisition-aware replays matched.
The complete journal has 14,042 acquisitions and 962,618 observations, including
two salvaged acquisitions/80 observations in the post-session drill minute.
The ordinary-session portion has 14,040 acquisitions and 962,538 observations.
The journal replay SHA-256 is
`b88ab9a5212a80d8aa2e3f0afde37d0a73b0b520294b6f63c97470b49807424d`.
Audit and two replays took 172.676 s; the whole local run took about 29 minutes.

The full-scale candidate and final delivery have 70 identical runtime files
(all market-data source, pinned Coordinator source and history entry/worker
code), checked by the accompanying hash inventory. The final archive is also
qualified separately by its focused tests and short proof.

An earlier candidate caught an inclusive audit-cutoff error: the first Uni
slot after the regular session was inadvertently expected. The cutoff was
corrected to end minus one microsecond before the successful scale proof.
A separate delivery rehearsal overlapped the large prefill and FAILED with
one Focus and two Hot missed slots. Focus/Hot both recorded a before-API gate
lateness of approximately 3.48 s; Hot also skipped the intervening slot. The
exact operating-system cause is not established. That failure was retained;
the final archive required an isolated rerun. No deadlines or guards were
relaxed, and no catch-up requests were introduced. Run one review experiment
at a time.

86 focused tests pass, including assessment, real-journal read-only
reassessment, acquisition, incremental guards and provider-harness checks.
The delivered archive runs 63 relevant offline tests before its short proof.
The owned stop-command drill stopped only its synthetic experiment.

These are synthetic local results. No real provider payloads, credentials,
private host transcripts, production journals or market-capacity claims are
included here. Native Windows acceptance, longer real-provider operation,
live Nasdaq transport/first-seen latency and production settings remain open.
