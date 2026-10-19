# Local validation and qualification boundary

The native acquisition and incremental guard modules are unchanged from the
accepted combined-synthetic baseline. This increment adds a separate provider
harness, read-only proposal loading, explicit actual-UTC mode, durable global and
per-channel quote budgets, credential delivery by process stdin, and bounded
operator entry points. Pinned Coordinator source remains commit
0417b345e2725f4e1e184d3b3202bedb846dbfca; no Coordinator implementation change.

- New focused suite: 23 tests PASS. Covers roster/session/hash binding, shared
  concurrent budget exhaustion and immutable reservations, deadline/stop gates,
  authorization/rate-limit stops, redaction, encrypted-config resolution, safe
  source paths, isolated worker import startup, and the actual-UTC native collector
  with a mocked provider transport. No real credentials or network are used.
- Extracted package: 93 tests PASS in 3.666s, then a 60-second four-process elapsed
  synthetic proof PASS: Uni 2/2, Focus 4/4, Hot 30/30, zero misses, 38 batch calls,
  2488 observations, eight revisions, correct final 555/40/40, audit PASS and
  identical journal replay. Replay SHA-256:
  580dcd5cdba00b0278acfc9c39891b9ed718b2eeec427ff4d6f79bc9d832ea21.
- Five-minute fresh native-filesystem rehearsal PASS: Uni 10/10, Focus 20/20,
  Hot 150/150; zero misses; 190/190 permitted batch calls; audit/repeated replay
  and lifecycle/in-flight checks PASS. Replay SHA-256:
  edbf3b750221bf23eba5c98809df3bca368de8edd97a3b0a0589f6bb7358d22a.
  Complete-cycle mean/p95/max ms: Uni 552.180/637.443/637.443;
  Focus 250.432/273.335/285.518; Hot 304.363/330.122/1158.788, including the
  intentional 1.1s synthetic overlap. No live provider timing is claimed.
- Independent owned-process stop drill PASS: after the first acquisition, the
  external stop command returned the runner successfully in 0.265s; Coordinator,
  Uni, Focus and Hot all reported stopped. Native Windows Ctrl+C and authenticated
  client shutdown still require on-host confirmation.

Two preliminary parallel workspace rehearsals stopped at a worker-exit check.
The exact cause was not established; no storage/coverage guard was relaxed.
A fresh extracted run passed all 93 tests and its complete short proof. The
longer rehearsal uses a fresh native temporary filesystem. These are retained
validation limits, not evidence of a Schwab or production polling failure.

The final archive is checked for exact shipped source bytes, CRLF launchers,
path confinement and absence of runtime databases/gates/credentials. Only generic
code and synthetic validation summaries are public. Captured provider payloads
and private operator transcripts are excluded.

Native Windows verification and the actual authenticated combined run remain
PENDING. The operator first runs the new package's offline verification, then
separately launches its five-minute provider command. The earlier authenticated
Hot-only result does not substitute for this combined measurement. No main merge,
production install, routine Hot activation, live Nasdaq latency, full-session
history capacity or production-default adoption is claimed.
