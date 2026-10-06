# Live Observation Overlay: quotes and membership

Status: experimental offline prototype, October 6, 2026. Extends the completed
historical Overlay at `5b47a2d`; production deployment and market-hours acceptance
are pending. The completed controlled 13:30 r2 experiment is evidence for the
publisher and poller behavior, not acceptance of this new viewer.

## First increment

Follow one schema-v2 daily journal and render one symbol's quote outcomes and
membership bands using the existing Overlay projectors and chart controls.
No market credentials, Schwab requests, membership decisions, or journal writes
belong to the viewer. Its database connections use SQLite `mode=ro`,
`query_only=ON`, and short explicit read snapshots. The optional demo has a
separate, explicitly synthetic writer and a new isolated journal.

The existing historical launcher, journal schema, poller, and publication
procedure retain their behavior. An absent candle input is represented by
`cache=None`, with no invented Schwab provenance, raw hash, or candle series.
The page says that candle acquisition is pending. Price mode works immediately;
Percent mode needs a selected reference until a real candle series exists.

## Discovery and canonical visibility

There are two separate orders:

1. Discovery follows SQLite insertion rowids in `sampling_membership_revision`
   and `quote_acquisition`, with an independent cursor for each table.
2. Projection follows the existing historical replay sort: effective time for
   complete hierarchy revisions, complete-acquisition time for quotes, membership
   before quotes at equal times, and the same deterministic remaining tie breaks.

Discovery never filters by availability timestamps. For example, a Uni
acquisition can complete at 13:30:02.47 and commit after a Focus acquisition that
completed at 13:30:05.3. Both are discovered, and the canonical projection places
Uni first. The Uni acquisition retains the revision bound at its scheduled slot;
the viewer does not reassign it to the newly effective hierarchy.

Published future revisions can be discovered immediately but are not applied
before effective time. Quote outcomes are not applied before complete-acquisition
time. Uncommitted headers, members, and observations are invisible. A complete
quote event must contain every requested-symbol outcome. Invalid, missing, and
request-error outcomes are evidence; a `quote` status is not a freshness claim.

## Catch-up, delayed commits, and failures

Each refresh discovers at most the configured number of headers per table and
loads at most that many complete events for projection. The default is 32. A
read snapshot is released before rendering or waiting for the next refresh.
The controller retains immutable event headers, latest channel state, and
selected-symbol evidence; it does not retain all Universe quote observations.

Stable event IDs prevent identical redelivery from adding points. Conflicting
content for an existing ID is an integrity error. When discovery inserts an
event before already projected events, rebuild through the requested cutoff in
bounded batches. Retain the previous coherent display until that reconstruction
can be published. Show `Catching up` during discovery or projection backlog.
An incremental display batch is staged before it becomes visible.

On a transient SQLite failure, retain the displayed state, show a source error,
and retry on subsequent ticks. Inconsistent durable evidence cannot be silently
skipped. The read cursors carry stable anchors, and the reader checks the store
identity. Changed identity, a missing anchor, or changed acquisition content
requires operator investigation rather than silent continuation.

Rowid cursors depend on the existing append-only journal convention. Replacing,
deleting from, or VACUUMing a journal while following it is unsupported. Anchor
checks are useful guards, not a general audit of all historical rows. The existing
journal audit remains the authoritative integrity check.

## Display controls and recovery

- **Live:** project discovered events through the session-bounded live clock.
  Absence of new quotes does not mean the journal or market has finished.
- **Pause:** freeze the inspection cutoff. Discovery continues. Evidence committed
  later with canonical availability at or before that cutoff can refine the view.
- **Seek / Beginning / Step:** inspect an inclusive historical cutoff. Seeking
  into the future is rejected. Beginning selects midnight ET of the session.
- **Return to Live:** catch up from the journal and resume following the clock.
- **Stop server / Ctrl+C:** stop this viewer and, in demo mode, its own synthetic
  writer. Production poller lifecycle is independent.

A fresh viewer starts discovery at zero and reconstructs from durable evidence.
The in-memory cursor is not a durable checkpoint and must not be reused without
its corresponding event history. Browser reconnect to the running server uses
the current controller snapshot. Controls are shared by clients of one server,
as in the existing historical dashboard; independent per-browser controls are
outside this increment.

Replay equality means equal canonical state and selected-symbol evidence at the
same cutoff after catch-up. It does not reproduce every intermediate screen
render or journal-write delay; schema-v2 quote visibility is completion-based.

## Acceptance

Offline acceptance exercises real SQLite transactions and the existing store,
replay reader, state projector, Overlay projector, and Dash callbacks:

- Delayed older completion discovered after a later-completing acquisition;
  canonical order restored with no duplicate selected-symbol points.
- Uncommitted acquisition hidden, then recovered after commit with all outcomes.
- Future membership and quote completion become visible at the inclusive boundary.
- Pause continues discovery; seek reconstructs an earlier hierarchy; Return to
  Live and a fresh process converge to historical replay.
- Missing observations, changed cursor anchors, and replaced identity fail
  visibly; transient read failures retain state and recover.
- The no-cache chart and live controls render through real Dash HTTP callbacks.
- Existing historical and production-related tests continue to pass.

Before a genuine market-hours experiment, review the chosen journal/symbol,
viewer launch and stop procedure, read latency and poller behavior, and exact
live-versus-replay checks. Full-day load and Windows WAL interaction are still
acceptance gates. Passing the offline prototype does not authorize attaching it
to the running production journal.

## Coordinator publication design alongside this increment

The viewer consumes complete revisions; the Watchlist coordinator owns decisions.
The coordinator needs a three-tier model and a serialized publication pathway,
extending its current flat watchlist/intent model. A proposed action envelope is:

| Field | Purpose |
| --- | --- |
| action_id / source_event_id | Durable idempotency and causal link |
| actor / source / received_at | Who or what initiated the decision and when known |
| action / symbol / reason | Requested membership operation and explanation |
| expected_parent_revision / parent_hash | Detect intervening revisions before deriving a new bundle |
| requested_effective_at / policy_version | Apply the stated timing and decision rules |
| outcome / published_revision / content_hash | Trace rejection, retry, or successful publication |

Serialize actions against the latest accepted hierarchy. A stale parent requires
reconciliation with intervening actions; it cannot overwrite them with an older
snapshot. Retry must recover a recorded outcome or safely retry the same action.
Publication remains one immutable atomic bundle preserving
`Hot ⊆ Focus ⊆ Uni`, with nonretroactive effective time and poller-bound revision
evidence. Display can link each transition to source, reason, and revision now;
the proposed richer action trace requires a later coordinator implementation.

Proposed manual semantics for review:

| Operation | Result |
| --- | --- |
| Ensure Uni | Add Uni; preserve existing higher tiers |
| Promote Focus | Ensure Uni and Focus |
| Promote Hot | Ensure Uni, Focus, and Hot |
| Remove Hot | Remove Hot; preserve Uni and Focus |
| Remove Focus | Remove Focus and Hot; preserve Uni |
| Remove Uni | Remove all three tiers |

## Next inputs and unresolved policies

Next, add separate durable live candle versions and confirmed Nasdaq halt
receipts. Captured candle versions are visible no earlier than both interval
close and receipt. Halt source event time and first-receipt time are separate;
later resumption/correction records preserve prior knowledge. Retrospective
historical caches and annotations remain distinguishable from captured live
evidence. Capture and display can precede automatic membership admission.

Initial halt admission proposal: qualifying newly received confirmed `LUDP/M`
halts ensure Uni and Focus, leave Hot unchanged. Qualifying codes, unavailable
symbols, capacity, repeated events, resumption behavior, and interaction with
manual removal still require policy decisions. No automatic admission is
implemented here. Hot has membership/display support but no agreed acquisition
cadence or admission policy.
