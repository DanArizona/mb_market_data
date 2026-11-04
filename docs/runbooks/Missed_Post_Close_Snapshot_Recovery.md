# Missed post-close preparation: exception and recovery runbook (experimental)

**Purpose.** A closeout that misses the completed trading session's **Eastern
calendar date** must not be treated as successful or silently reconstructed
from a subsequent day's cumulative quote volume. Recovery must preserve
historical evidence, the exact original failure, and trading-session
provenance. This is an operator decision, not an automatic fallback.

## Time and evidence contract

For the currently qualified regular-day snapshot implementation, the
snapshot must be acquired on the completed session date, after 16:00 ET,
before midnight ET. (An actual exchange early close is a separate
qualification item: the 16:00 hardcoded validator and reminder must both
be made exchange-calendar aware before shortening this window.) The
following day's `totalVolume` is **not a substitute**.

`NEXT SESSION PREPARATION: READY` requires the same-day source workflow,
all immutable artifact hashes, the opening roster/r0 and matching
target-session journal publication. If that evidence is absent or inconsistent,
report `INCOMPLETE` or `RECOVERY_REQUIRED`; do not label it PASS.

## Triage: first assess, read-only

Use the experimental read-only assessment script with explicit dates:

```cmd
python probes\assess_closeout_recovery.py --closed-session YYYY-MM-DD --next-session YYYY-MM-DD --market-data-root C:\Users\danla\Documents\github\mb_market_data
```

Inspect whether the dated symbol directory, market snapshot raw acquisition,
manifest, deterministic decision ledger, opening r0 and target journal actually
exist. Check any acquisition timestamps, source session date, evidence hashes,
and whether a published revision already exists. Never delete, overwrite or
backdate saved evidence. Never equate one directory's existence with validity.

## Case A: still the source date, before Eastern midnight

1. If no workflow has started and the source session has closed, invoke the
   authorized attended closeout immediately. It will ask for the encrypted
   Schwab configuration password once.
2. If any partial workflow output exists, **do not** rerun the normal builder
   over it. Review which immutable stages completed. A new, separately
   authorized suffixed attempt is required if acquisition must be repeated
   while source-date validation is still available. Its provenance and
   manifest must be explicitly reviewed before publication.
3. Publish the accepted target r0 only after verifying the source-date evidence
   and before the target revision's intended effective time. Audit/replay
   the unopened target journal. Audit/replay the completed source journal
   independently.
4. If reconstruction or publication cannot be qualified before midnight,
   leave status INCOMPLETE. Preserve all artifacts and the exception.

## Case B: after Eastern midnight, complete dated snapshot was retained

1. Do not attempt a new source-date quote acquisition. Inspect the retained
   snapshot and its raw acquisition receipt, timestamps, source session
   matches and hashes.
2. A separately qualified offline continuation may build from existing
   immutable saved evidence without acquiring new quotes. Neither the current
   normal builder nor this diagnostic automatically completes a partial
   workflow. Do not overwrite its directory.
3. Verify the resulting decision ledger, roster and proposed r0. Publish only
   prospectively if the target's 09:30 ET effective time has not passed and
   accepted publication rules permit it. Preserve the original incomplete
   closeout incident and document any exceptional reconstruction.
4. Re-run audits and replay, without replacing earlier verdicts.

## Case C: after Eastern midnight, no valid source-date snapshot exists

**The missing market snapshot cannot be recreated as though it had been
acquired the previous day.** Report `MISSED_POSTCLOSE_SNAPSHOT` and
`RECOVERY_REQUIRED`.

A bounded fallback to avoid an unnecessary next-morning outage may be
considered, but is **never automatic**:

1. Find the most recent *verified* eligible Uni roster with immutable
   source artifacts and hashes. Record its true source session date,
   age, symbol count, and why the latest daily selection is unavailable.
2. Obtain **explicit operator approval** for an exceptional carry-forward.
   Use a purpose-built or reviewed recovery procedure that records the
   older source and reason in the new target session's proposed r0 metadata.
   Never claim that the new roster came from the missed completed session.
3. Apply ordinary current-day safety checks (e.g. listed symbols, invalid
   quote responses, membership nesting and publication eligibility).
   Do not infer that a stale roster has become fresh.
4. Publish the target r0 **before** its 09:30 ET effective time, with
   immutable provenance and the accepted expected Uni/Focus/Hot structure.
   Do not backdate an after-open revision to the opening time.
5. Run fresh next-session API overnight-volume acquisition for that target
   trading day, build and verify Focus r1, publish ahead of its effective
   time, then start the separate normal Uni/Focus pollers only if qualified.
6. Report **RECOVERED_WITH_EXPLICIT_CARRY_FORWARD**, not standard
   post-close preparation PASS. The original source-data loss remains
   a real historical failure.

If an acceptable prior roster, approval, or timely publication is missing,
stop escalation at `NOT_READY`. Do not silently start production with
unverified or backdated membership. Use the established incident escalation
and recovery process rather than inventing data.

## Case D: exceptional market closure, machine failure or outage

Confirm the exchange's official session schedule and whether that day
actually traded. Do not assume a weekday was a regular close. If there
was no trading session, a closeout snapshot may legitimately not be due.
If the machine was powered down, notifications cannot be guaranteed; treat
the next attended inspection as a missed-workflow triage.

In all cases preserve a dated incident record with:
- exact source and target sessions and official exchange close time;
- the first discovery time in ET, operator disposition and approval;
- which snapshots/manifests/revisions exist and their hashes;
- why normal preparation was missed or blocked;
- any older roster provenance and the separately accepted recovery verdict;
- exact audit/replay results for both source and target.

## Current implementation limits

The read-only `probes/assess_closeout_recovery.py` provides classification.
The carry-forward path is an **operator-approved recovery procedure**, not a
new automatic code path in this experimental branch.

The current reminder and post-close snapshot validator have NOT yet been
qualified for early closes. The current scheduler checks hourly all day,
while the Python reminder suppresses warnings until the fixed 16:10 ET
threshold and after midnight. An official calendar-aware gate and observed
reminder delivery/acknowledgment still need design and offline Windows tests.
No Windows task, notification account or production runner has been activated
by this document.
