# Daily Hot acquisition API — development candidate

`daily_hot_acquisition.DailyHotPoller` is the daily adapter around the durable
`sampling_acquisition` dispatch/response/commit/ACK engine. It requires the
existing daily publication claim, matching r0/r1 hashes, interleaved Uni/Focus
cadence, explicit Hot seconds, a regular-session collection window and a trusted
publication validator that explicitly returns True. The caller also asserts
activation authorization; this assertion is an internal API boundary, not a
user-authentication or deployment mechanism.

The engine is ported from the already accepted isolated native implementation.
`ExplicitPollSlot` preserves its three-positional-argument API with a custom
frozen initializer because the production `PollSlot` now also has an optional
cadence field. The isolated poller still rejects production opening journals
and still requires its original experimental marker.

The daily adapter does not initialize or replace opening membership. It writes
only its Hot sidecar/run/outcomes, using the original engine's owner lock,
complete response recovery, unknown-dispatch abandonment and receipt checks.
The Coordinator facade supplies lifetime collector ownership, one-symbol and
cumulative quote-call limits, source/publication checks and provider-stop
latching. An owning authenticated controller and read-only worker adapter
remain required before live use. There is no production Hot CLI here.

The accompanying synthetic qualification uses an explicit ten-second tail
window and actual process-exit recovery drills. It establishes no full-day
provider capacity, live-feed result or authenticated controller readiness.
