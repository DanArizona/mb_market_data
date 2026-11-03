# Next-session closeout readiness gate (experimental)

**Problem:** November 2 production closeout was declared complete after the
current-session audit/replay and Health shutdown, even though November 3
post-close Universe preparation was absent. This repeated a documented
operational failure. The required corrective behavior is **fail-closed**.

## Operating contract

A completed session has **two independent closeout verdicts**:

1. Today's polling journal: full-session audit and replay, including explicit
   FAIL if any real slot is missing. Never fix or suppress these gaps.
2. Tomorrow's opening: a same-day post-close source snapshot, deterministic
   Universe, immutable workflow artifacts, an opening r0 effective at 09:30 ET,
   and a distinct schema-v2 journal with matching published r0.

**Do not say "production closeout COMPLETE" until both have been checked and
their distinct results reported.** A current-session FAIL may remain a finalized
closeout with findings; it must not become PASS. A missing next-session build
must be reported `NEXT-SESSION PREPARATION: INCOMPLETE`.

## New attended closeout entrypoint (review candidate)

The new `probes/prepare_next_session_closeout.py` invokes the **existing**
production `run_daily_universe_production.py --publish-journal-root` five-stage
workflow: Nasdaq directory, **one interactive Schwab password prompt** for the
post-close quote snapshot, deterministic Universe selection, opening r0
proposal, and r0 publication in the next-session schema-v2 journal.
It then verifies the saved artifacts, dates, proposal hash, publication and
replay. The wrapper does not ask for a second password.

It **refuses to start a new build outside 16:00–24:00 ET on the completed
session date** and reports `INCOMPLETE` without launching a provider request.
An existing workflow directory is never overwritten or silently regenerated.
If it already exists, the wrapper verifies it without invoking the builder; a
partial or damaged directory requires an explicit review/recovery instead of
silent retry. It does not declare today's journal audit PASS.

Intended command on the **completed session date**, after 16:00 ET:

```cmd
python probes\\prepare_next_session_closeout.py --closed-session 2026-11-03 --next-session 2026-11-04
```

Put that command **last in a multi-command CMD block**, because the builder
prompts for a password. This candidate is not installed or Windows-qualified.
A separately reviewed scheduled after-close reminder and an overall
two-verdict closeout gate are still required so the handoff cannot be
overlooked.

## Guarded usage (review candidate; not installed in production)

After the mandatory, interactive post-close workflow and r0 publication, run:

```cmd
python probes\verify_next_session_readiness.py --closed-session 2026-11-03 --next-session 2026-11-04 --market-data-root C:\Users\danla\Documents\github\mb_market_data
```

The verifier is read-only, checks exact source/target dates, all seven recorded
artifact hashes, nonempty Uni/empty Focus/Hot r0, a matching published r0 in
the **next date's own** schema-v2 journal, and complete journal replay.
`READY` exits 0; missing or inconsistent evidence prints `INCOMPLETE`
and exits nonzero. An explicit carry-forward requires its own separately
reviewed provenance policy; this initial checker intentionally rejects it as
a normal same-day build.

Do not use this guard as a substitute for the separate **today** audit/replay,
or claim that it acquired a snapshot. Do not prefill or relabel missing previous
session evidence. Do not run the normal builder after ET midnight for the
previous session.

## Remaining deployment work

This is **only the first, non-production gate**. Before production adoption:
- Execute and review the committed offline tests for missing build, corrupt hash,
  incorrect date, absent r0, hash mismatch, valid complete fixture, and attended
  runner failure/retry behavior.
- Wire the exit code into a dated closeout command or supervisor which cannot
  print overall COMPLETE while next-session readiness is INCOMPLETE.
- Implement timely post-close prompting/scheduling with credential handling.
  The interactive Schwab password makes silent unattended execution unsafe:
  an alarm or attended runner may be necessary, not an implicit vault bypass.
- Windows-qualify the exact candidate; review and explicitly approve
  production installation, the operating schedule and any credential flow.
- Retain the original November 2 omission and November 3 Focus FAIL.

Until deployment, the operator must explicitly run preparation and verify
tomorrow's r0 as part of the ordinary closeout, as was done for November 4.
