# Historical Seek — Implementation Plan

Status: ready for implementation; no source changes made  
Scope: single-day quote-journal diagnostic replay  
Prepared: 2026-09-15

## 1. Outcome

Add a dashboard **Seek** operation that reconstructs the projected state for one selected journal day at a user-entered Eastern Time (`HH:MM:SS`). The journal remains selected at dashboard launch from the command line. Seek is an investigative control, not a long-horizon backtesting interface.

The first implementation will favor correctness and auditability over optimization: each seek creates a fresh projector and replays the journal's deterministic event stream from the beginning through an inclusive cutoff.

## 2. Fixed behavioral contract

| Concern | Required behavior |
|---|---|
| Event availability | A channel revision is available at `effective_at_utc`; an acquisition is available at `completed_at_utc`. |
| Inclusion rule | Apply every event with `available_at_utc <= target_utc`. |
| Whole-second input | `HH:MM:SS` means the end of that displayed second: `HH:MM:SS.999999` ET. |
| Before first event | Valid. Show the requested replay time and an empty projected state. |
| Between events | Valid. Show the requested replay time; projected state remains as of the last included event. |
| At an event time | Include that event. Existing deterministic tie ordering remains authoritative. |
| At or after last event | Reconstruct the complete state and report end of recorded data. |
| Playback after seek | Never auto-resume. Preserve the selected speed. |
| Reverse movement | Rebuild from a fresh projector; do not attempt to undo projected state. |
| Journal boundary | Reject a target whose ET calendar date differs from the journal session date. |
| Legacy channels | Replay current independent-channel journals faithfully; do not impose hierarchy rules they did not record. |

`replay_time_utc` represents the requested cursor position. The projected state's own `current_time_utc` remains the availability time of the most recently applied event. This distinction is intentional when a target falls between events or after the last event.

## 3. Source changes

### 3.1 `src/mb_market_data/quote_dashboard_replay.py`

Add a public method immediately after `restart()` and before `set_speed()`:

```python
def seek(self, target_utc: datetime) -> DashboardReplaySnapshot:
    ...
```

The method will:

1. Require a timezone-aware `datetime`, normalize it to UTC, and reject a target outside the reader timeline's ET session date.
2. Acquire the controller lock.
3. Retain the current speed.
4. Reset the event factory, iterator, pending event, and projector by using the controller's existing reset path.
5. Apply pending events while `pending.available_at_utc <= target_utc`.
6. Set the replay position to the requested target even when no event exists exactly there.
7. Clear playback anchors so elapsed wall time from the former position cannot affect later playback.
8. Leave the controller non-playing:
   - `PAUSED` when a later event remains;
   - `COMPLETE` when no later event remains.
9. Restore the retained speed and return the resulting snapshot.

This uses the existing event factory supplied by `QuoteJournalReplayReader.events`, so each reconstruction obtains a new ordered stream and a new `QuoteEventStateProjector`.

No change is needed to the event ordering algorithm. `_event_sort_key()` already orders on availability time and resolves same-time ties deterministically, with revision events before acquisition events.

### 3.2 `src/mb_market_data/quote_dashboard.py`

Add a small pure parser after `_control_values()` and before `create_quote_dashboard()`:

```python
def _parse_seek_time_utc(session_date: date, value: str) -> datetime:
    ...
```

Initial accepted format: exactly `HH:MM:SS` in ET. The parser will:

- trim surrounding whitespace;
- validate hour, minute, and second ranges;
- combine the value with the journal `session_date` in `America/New_York`;
- set `microsecond=999999`;
- return an aware UTC datetime;
- raise a clear `ValueError` for missing or malformed input.

Add two controls inside the existing replay-control area, following **Restart**:

- a text/time input, id `replay-seek-time`, with placeholder `HH:MM:SS`;
- a button, id `replay-seek`, labeled **Seek**.

Extend `update_replay()` with:

- `Input("replay-seek", "n_clicks")`;
- `State("replay-seek-time", "value")`;
- a `replay-seek` trigger branch that parses the input and calls `controller.seek()`.

For valid input, existing outputs already cover the necessary UI refresh: status, replay clock, progress, state metrics, channel cards, and grid rows. A seek that lands between events may leave `state.event_count` unchanged; the replay clock and progress must still update, while the materialized state correctly remains unchanged.

For invalid input, keep the last valid controller state and return a concise validation message in the replay status area. Do not throw a server-visible callback error and do not partially move the replay position.

Display wording should distinguish:

- `Paused` for a target before the end with later events available;
- `End of recorded data` when the target is at or after the last event;
- the existing state timestamp as the last event actually incorporated.

### 3.3 `tests/test_quote_dashboard_replay.py`

Insert seek tests after `test_restart_and_finish_rebuild_state_from_the_source` and before the invalid-speed test.

Required controller cases:

1. **Before first event** — zero applied events, requested replay position retained, status paused.
2. **Exact event boundary** — the event at the target is included.
3. **Between events** — only the preceding prefix is applied; requested cursor time is retained.
4. **Backward seek** — after reaching a later state, seeking earlier reconstructs the correct smaller prefix with a fresh projector.
5. **After last event** — all events applied, status complete, requested cursor retained.
6. **Paused after seek** — seeking while playing does not resume playback.
7. **Speed preservation** — selected speed is unchanged across seek.
8. **Invalid target** — naive datetime and wrong ET session date are rejected without state mutation.
9. **Repeatability** — seeking to the same target twice produces equivalent snapshots.

The synthetic three-event fixture is sufficient for these behavioral tests.

### 3.4 `tests/test_quote_dashboard.py`

Add parser tests near the existing layout tests:

- normal market time converts from ET to UTC and ends in `.999999`;
- midnight and `23:59:59` are accepted for the selected session date;
- blank, malformed, and out-of-range values are rejected;
- DST conversion is based on `America/New_York`, not a fixed offset.

Extend the layout test to assert that `replay-seek-time` and `replay-seek` exist.

Add callback tests for:

- a valid seek updates replay status, clock, progress, metrics, cards, and grid;
- an invalid input leaves the previous replay snapshot intact and produces a useful message;
- a seek to a time between events updates the cursor without falsely advancing state.

Update the expected callback input count only where the current tests assert it directly.

## 4. Files intentionally unchanged

- `src/mb_market_data/quote_journal_replay.py` — availability semantics and deterministic ordering already match the seek contract.
- `src/mb_market_data/quote_event_state.py` — the forward-only projector is already the correct reconstruction primitive.
- `src/mb_market_data/quote_dashboard_view.py` — it already builds a view from any immutable projector snapshot.
- `src/mb_market_data/quote_observation_store.py` — no schema or query change is needed for a correct linear implementation.
- `probes/dashboard_quote_observation_journal.py` — journal/day selection remains a command-line responsibility.
- `probes/replay_quote_observation_journal.py` — command-line immediate/paced replay behavior is unaffected.

## 5. Real-journal acceptance checks

After unit tests pass, exercise the dashboard against both collected days:

- `2026-09-14.sqlite3`
- `2026-09-15.sqlite3`

For September 15, verify at minimum:

1. Before `09:30:00` ET: no events applied.
2. `09:30:00` ET: both `r0` revisions included because whole-second input maps through `.999999`.
3. A point between two completed acquisitions: event count and state stop at the earlier completion, while the replay clock shows the requested time.
4. A backward jump from late afternoon to the morning: state equals a clean replay to the same morning cutoff.
5. `15:59:50` ET and later: 2,342 events applied; 598,260 observations represented by the completed replay; state shows focus `r0` with 4 members and uni `r0` with 759 members.
6. Repeated seeks to the same target yield identical projected state and provenance.
7. Play after a seek advances from the requested cursor using the retained speed; seek itself never starts playback.

The command-line replay's existing sequence hash remains a useful whole-day regression reference, but seek validation should compare projected prefix state rather than inventing a new hash format in this increment.

## 6. Performance and operational risk

The September 15 immediate replay processed the full day in about 15.5 seconds on the observed machine. Therefore a near-close linear seek may block the dashboard callback for a similar duration. That is acceptable for the first correctness-oriented implementation only if the UI clearly shows that work is in progress and normal interaction resumes cleanly.

Measure three positions on the real journal: near open, midday, and near close. Record wall time and peak memory. If late-day seek latency is unacceptable, optimize in a separate increment after the behavior is locked by tests.

Preferred later optimization order:

1. In-memory immutable checkpoints at a measured event interval.
2. Rebuild from the nearest earlier checkpoint and replay the remaining suffix.
3. Only if still necessary, consider store-level as-of queries or persistent checkpoint data.

Do not change journal schema or projector semantics merely to optimize the initial seek.

## 7. Deferred work

- Dashboard journal/date selector.
- Cross-day navigation or multi-day seek.
- Historical-market-data backtesting from Schwab, Massive, or another provider.
- Fractional-second input in the dashboard.
- Keyboard scrubbing, sliders, bookmarkable timestamps, or playback ranges.
- Checkpoint acceleration until real measurements justify it.
- Enforcing `Hot ⊆ Focus ⊆ Uni` during replay. Current journals are legacy independent-channel captures. Hierarchy validation should begin only when a durable journal contract or metadata marker identifies hierarchy-governed data.
- Symbol removal policy (`FORCE_ABSENT`, reversible removal, timed removal, or related states).

## 8. Implementation order

1. Add controller tests for seek semantics; confirm they fail for the missing API.
2. Implement `QuoteDashboardReplayController.seek()` and make controller tests pass.
3. Add parser tests and implement the pure ET parser.
4. Add dashboard controls and callback handling; update dashboard tests.
5. Run the focused replay, state, and dashboard test modules.
6. Run the complete test suite.
7. Perform the September 14 and 15 real-journal acceptance checks and record timings.
8. Update the roadmap item with test evidence and measured latency; decide separately whether checkpoint optimization is warranted.

## 9. Completion gate

Historical seek is complete for this increment when:

- every inclusive-cutoff boundary case is covered by automated tests;
- backward seek demonstrably reconstructs rather than mutates state in reverse;
- invalid input cannot move or corrupt controller state;
- seek never auto-resumes and preserves speed;
- both real journals pass the acceptance checks;
- no schema migration is introduced;
- observed latency is documented, with any optimization decision explicitly deferred or scheduled.

## Recommended first action

Write the controller-level boundary tests in `tests/test_quote_dashboard_replay.py` before changing production code. They fix the inclusive cutoff, backward reconstruction, status, and speed-preservation contract at the narrowest layer.
