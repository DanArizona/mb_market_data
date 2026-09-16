# Atomic Hierarchical Membership Contract and Implementation Plan

**Status:** Approved design; implementation not started

**Design date:** 2026-09-16

**Applies to:** `mb_market_data` sampling membership and its future coordinator publication adapter

**Implementation status (2026-09-16):** Slice A is implemented, including
schema-v2 audit support. Slice B code paths are implemented through controlled
publication, slot-time membership resolution, acquisition binding, fail-closed
lookup, and durable empty-channel skip evidence. Store and audit enforcement
also require the latest revision effective at each slot and validate
per-channel provenance/content hashes. The concurrent live-like r0-to-r1
acceptance run remains pending. Slice C remains pending.

## 1. Outcome

Introduce live membership changes without ever exposing a partially updated sampling hierarchy. Each committed revision is one complete, immutable snapshot of:

**Hot ⊆ Focus ⊆ Uni**

Every acquisition binds to the exact committed revision and channel membership it used. Historical journals from September 11, 14, and 15 remain replayable under their original independent-channel semantics.

This work does not add removal-duration categories, Hot polling policy, a live dashboard transport, a dashboard date selector, or multi-day backtesting.

## 2. Ownership boundary

| Concern | Owner |
| --- | --- |
| Producer intents, expiry/cancellation, precedence, and desired canonical state | `mb_watchlist_coordinator` |
| Complete Uni/Focus/Hot projection supplied for publication | Coordinator-side sampling-hierarchy adapter |
| Hierarchy validation, atomic journal commit, current-revision lookup, acquisition binding, replay, and audit | `mb_market_data` |
| Quote acquisition | `mb_market_data` pollers |
| ToS mutation and reconciliation | Existing coordinator/ToS adapter path |

The coordinator's current `CanonicalWatchlist` is a single flat set and should not be reinterpreted as the three-level sampling hierarchy. Add a separate future `CanonicalSamplingHierarchy` projection or adapter contract. Likewise, the coordinator's GUI `MaterializationTransaction` is not the sampling-journal transaction.

## 3. Domain contract

Add an immutable `SamplingHierarchyRevision` value with:

- `session_date`
- global `revision` number
- `effective_at`
- store-assigned `published_at`
- complete ordered `uni_symbols`, `focus_symbols`, and `hot_symbols`
- `source`, `reason`, and optional `metadata`
- contract identifier `nested-uni-focus-hot-v1`
- one deterministic content hash over the canonical proposal and all three normalized sets; the store-assigned `published_at` is excluded

Rules:

1. A revision contains complete desired sets, never deltas.
2. Symbols are trimmed, upper-cased, de-duplicated, and ordered deterministically before validation or hashing.
3. Uni must be non-empty. Focus and Hot may be empty.
4. `Hot ⊆ Focus ⊆ Uni` is validated before any write.
5. Invalid nesting is rejected. The journal does not silently promote or demote symbols to repair producer output.
6. The first hierarchy-governed revision for a session is `r0`; later revisions are exactly sequential.
7. Effective times are timezone-aware and nondecreasing within a session.
8. Live publications are not backdated: `effective_at` must be at or after the store-assigned `published_at`. Publishing a future-effective r0 before the session is valid.
9. The same revision and same content are idempotent. The same revision with different content is a conflict. Stale or skipped revisions are rejected.
10. A membership revision may be created when membership or retained provenance changes; identical normalized content and provenance do not create a new revision.

Removal stays deliberately simple: omission from the next complete snapshot means the symbol is no longer a current member of that channel. Historical observations remain in the journal. `FORCE_ABSENT` remains a coordinator intent, not a journal membership state. No `REMOVED`, `REMOVED_5MIN`, or similar timed categories are introduced.

## 4. Atomicity and visibility

One SQLite transaction inserts the hierarchy header plus all three channel snapshots and their members. Readers see either the previous complete revision or the new complete revision; they never see an intermediate Uni-only or Focus-only state.

Only the coordinator-side publisher writes hierarchy revisions. Pollers are readers of membership and writers of acquisitions. A failed validation or failed database transaction publishes nothing.

For an ambiguous publisher outcome, re-read the target revision and content hash:

- exact match: treat the retry as idempotent success;
- no revision: retry the original publication;
- same revision with a different hash: stop as a conflict.

## 5. Acquisition semantics

Immediately before claiming and dispatching a slot, each poller reads the latest revision effective at that slot and captures the channel-specific symbols with the global revision number. The quote request must use `poll_request.symbols`, not the static startup list.

If no effective hierarchy revision exists, the poller fails closed rather than guessing membership. If a permitted channel is empty, the slot is explicitly recorded or reported as skipped; no empty Schwab request is issued.

An acquisition that began under `r0` remains valid if `r1` commits while it is in flight. Its completion is journaled against `r0`. Applying that old acquisition during replay updates observations but never rolls current membership back from `r1`.

## 6. Journal and compatibility model

Create schema version 2 for hierarchy-governed journals. Do not rewrite or migrate completed schema-version-1 daily journals in place. Journal creation selects the contract explicitly; Slice A leaves the existing poller's schema-v1 creation path unchanged, and Slice B switches the probe to the hierarchy contract only when dynamic capture is ready.

Schema v2 adds a `sampling_membership_revision` header keyed by `(session_date, revision)`. It retains per-channel revision/member rows so existing acquisition bindings remain explicit, but every global revision must contain exactly Uni, Focus, and Hot channel rows. Empty Focus or Hot rows are valid, so their member counts may be zero. All three rows share the global revision and effective time and reference the hierarchy header.

Writer behavior:

- new hierarchy-governed journals are schema v2;
- the v2 writer exposes `record_membership_revision` and disallows independent channel publication;
- completed v1 journals remain read-only evidence;
- no automatic in-place v1-to-v2 migration is performed.

Reader behavior:

- support schema v1 and v2;
- v1 produces the existing individual `ChannelRevisionEvent` objects and preserves legacy behavior;
- v2 produces one `MembershipRevisionEvent` containing the entire hierarchy;
- replay applies a v2 membership event in one projector operation, so dashboard Step cannot reveal a partial hierarchy.

The audit must distinguish `legacy-independent-channels` from `nested-uni-focus-hot-v1`. For v2 it verifies bundle completeness, member counts, nesting, sequential revisions, monotonic effective times, content hashes, and every acquisition's exact revision/symbol binding.

## 7. Exact implementation slices

### Slice A — Contract, schema v2, replay, and audit

No live polling behavior changes in this slice.

1. Add `src/mb_market_data/sampling_membership.py` with normalization, `SamplingHierarchyRevision`, hierarchy validation, and deterministic hashing.
2. In `quote_observation_store.py`, add schema-version compatibility constants, the v2 header schema, and `record_membership_revision` adjacent to the current channel-revision path. Preserve v1 reading; reject attempts to mutate a v1 journal through the v2 writer.
3. In `quote_journal_replay.py`, add `MembershipRevisionEvent` after `ChannelRevisionEvent` and schema-specific event loading.
4. In `quote_event_state.py`, add a prevalidated all-channel apply operation. Mutate projector state only after the whole bundle passes.
5. Extend `quote_journal_audit.py` with contract labeling and v2 hierarchy checks.
6. Update replay CLI output and README schema/replay descriptions.

Primary tests:

- normalization, empty Focus/Hot, and all nesting violations;
- r0, exact sequential revisions, idempotent retry, conflict, stale revision, gap, and time regression;
- transaction rollback after injected failure between channel writes;
- v2 replay exposes one atomic membership event;
- old in-flight acquisition remains valid without membership rollback;
- v1 Sep. 11/14/15 compatibility and unchanged accounting;
- v2 corruption and incomplete-bundle audit failures.

### Slice B — Dynamic poller handoff

1. In `polling_journal.py`, separate run registration from initial static-revision publication and expose latest-effective hierarchy lookup.
2. In `watchlist_polling.py`, add a membership-provider boundary and capture the selected channel from one hierarchy revision.
3. In `probe_universe_quote_watch.py`, resolve membership immediately before dispatch and pass `poll_request.symbols` to quote acquisition.
4. Add a small seed/publish CLI for controlled r0/r1 testing before coordinator integration.
5. Keep Uni and Focus in separate processes; add Hot only after this slice is proven.

Primary tests:

- concurrent Uni and Focus readers observe only complete r0 or complete r1;
- r1 becomes effective between slots;
- r1 commits while an r0 request is in flight;
- poller restart binds the current revision without republishing it;
- absent initial revision fails closed;
- empty optional channel skips safely;
- publisher crash before commit and retry after ambiguous acknowledgement.

### Slice C — Coordinator publication adapter and live validation

1. Add a coordinator-side `CanonicalSamplingHierarchy` projection; do not overload `CanonicalWatchlist`.
2. Publish complete hierarchy revisions through the v2 journal contract.
3. Validate an r0-to-r1 transition with concurrent Uni and Focus polling and dashboard replay.
4. Record exact commands, revision contents, acquisition bindings, counts, timing, restart behavior, and commit hash.

Hot cadence, capacity, admission/eviction, and API budget remain a later policy slice.

## 8. Acceptance gates

Slice A is complete only when the full suite passes and synthetic failure tests prove atomic visibility. Slice B additionally requires a concurrent live-like run with an in-flight old revision. Slice C requires a recorded real transition and exact replay of that transition.

The standing September 11, 14, and 15 schema-v1 journals must retain their original event/acquisition/observation accounting. No change may relabel them as strict hierarchy journals.

## 9. Later and deferred work

**Later:** Hot polling policy; live dashboard event delivery; coordinator persistence/recovery hardening; journal retention/distillation; operational health reporting.

**Deferred:** timed-removal states; dashboard date selector; multi-day seek; seek as a backtesting interface; complex source-priority or capacity policy.

## 10. Recommended first implementation action

Implement Slice A's `SamplingHierarchyRevision` contract and its pure unit tests first, before changing SQLite or poller code. This fixes normalization, empty-channel, nesting, revision, and hashing semantics at the narrowest boundary and prevents the storage schema from becoming the accidental specification.
