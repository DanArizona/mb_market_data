# Schema-v2 Live-like Transition Runbook — 2026-09-17

## Objective

Validate concurrent schema-v2 Uni and Focus polling across one atomic r0-to-r1
hierarchy transition. The critical case is the Uni slot scheduled for
12:00:00 ET:

- it resolves and dispatches under r0;
- r1 commits while that request is in flight;
- r1 becomes effective at 12:00:01 ET;
- the r0 request completes after r1 becomes effective;
- the acquisition remains durably bound to r0;
- later Uni and Focus slots bind to r1.

This test uses a dedicated journal root and does not alter the ordinary daily
schema-v1 collection journal.

## Controlled hierarchy

The proposal files are:

- `probes\evidence\schema_v2_transition_2026-09-17\r0.json`
- `probes\evidence\schema_v2_transition_2026-09-17\r1.json`

Both revisions contain ten Uni symbols. Focus contains four symbols under r0
and adds MSFT under r1. Hot contains SPY under r0 and adds MSFT under r1. The
publisher normalizes and validates **Hot ⊆ Focus ⊆ Uni** before committing.

## Before the test

Run from the repository root in the `sea-green` environment.

Publish r0 before starting either poller:

```cmd
python probes\publish_sampling_hierarchy.py output\quote_observation_journal_v2_transition_2026-09-17\2026-09-17.sqlite3 probes\evidence\schema_v2_transition_2026-09-17\r0.json
```

The result must report schema-v2 r0 with Uni=10, Focus=4, Hot=1 and
`Result: inserted`. An identical retry may report `already_present`.

## Timed run

Open three additional `sea-green` command windows before 11:58 ET. Keep any
ordinary full-day collection in its existing windows and journal root.

### Window 1 — arm r1 publication

```cmd
python probes\publish_sampling_hierarchy.py output\quote_observation_journal_v2_transition_2026-09-17\2026-09-17.sqlite3 probes\evidence\schema_v2_transition_2026-09-17\r1.json --publish-at 2026-09-17T12:00:00.100000-04:00
```

The command loads and validates r1, displays `publication armed`, waits, then
publishes. It must report an actual `Published at` earlier than the r1
`Effective at` of 12:00:01 ET.

### Window 2 — controlled Uni poller

Start this early enough to finish the password prompt before 11:58 ET:

```cmd
python probes\probe_universe_quote_watch.py --watchlist-kind uni --journal-root output\quote_observation_journal_v2_transition_2026-09-17 --journal-schema-version 2 --batch-size 1 --start-at 2026-09-17T11:58:00 --stop-at 2026-09-17T12:02:00 --output-root output\schema_v2_transition_quote_watch
```

The deliberately small batch size makes the ten-symbol 12:00:00 r0 request
remain in flight across the scheduled r1 commit and effective time.

### Window 3 — controlled Focus poller

Start this early enough to finish the password prompt before 11:58 ET:

```cmd
python probes\probe_universe_quote_watch.py --watchlist-kind focus --journal-root output\quote_observation_journal_v2_transition_2026-09-17 --journal-schema-version 2 --start-at 2026-09-17T11:58:00 --stop-at 2026-09-17T12:02:00 --output-root output\schema_v2_transition_quote_watch
```

Both pollers stop automatically at 12:02 ET.

## Validation

Audit the dedicated journal:

```cmd
python probes\audit_quote_observation_journal.py output\quote_observation_journal_v2_transition_2026-09-17\2026-09-17.sqlite3 --show-missing 20
```

Expected accounting, assuming every configured slot runs:

- audit result: `PASS`;
- hierarchy revisions: 2;
- acquisitions: Uni 8, Focus 16, total 24;
- observations: Uni 80, Focus 72, total 152;
- no missing slots or row-count mismatches.

Replay the journal:

```cmd
python probes\replay_quote_observation_journal.py output\quote_observation_journal_v2_transition_2026-09-17\2026-09-17.sqlite3 --progress-every 0
```

Expected replay totals are 26 events: two atomic hierarchy revisions and 24
acquisitions, containing 152 observations.

Finally, prove the in-flight ordering directly:

```cmd
sqlite3 -header -column output\quote_observation_journal_v2_transition_2026-09-17\2026-09-17.sqlite3 "SELECT a.channel, a.channel_revision, a.scheduled_at_utc, a.dispatched_at_utc, r.published_at_utc AS r1_published_at_utc, r.effective_at_utc AS r1_effective_at_utc, a.completed_at_utc, (a.dispatched_at_utc < r.published_at_utc) AS r1_published_after_dispatch, (r.published_at_utc < a.completed_at_utc) AS r1_published_before_completion, (r.effective_at_utc < a.completed_at_utc) AS completed_after_r1_effective FROM quote_acquisition AS a CROSS JOIN sampling_membership_revision AS r WHERE a.channel = 'uni' AND a.scheduled_at_utc = '2026-09-17T16:00:00.000000Z' AND r.revision = 1;"
```

The row must show `channel_revision = 0` and all three final Boolean columns
equal to `1`.

## Failure handling

If r1 publication is late, rejected, or the 12:00:00 Uni acquisition finishes
before r1 becomes effective, preserve the database and console output as failed
evidence. Do not relabel the run successful. Use a new dedicated journal root
for a later attempt so revision and acquisition history is never rewritten.
