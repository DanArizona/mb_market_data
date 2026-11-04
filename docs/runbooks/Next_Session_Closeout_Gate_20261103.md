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

## Unified attended closeout (review candidate; one password)

The top-level `probes/run_daily_closeout.py` is the intended single
operator command after the close. It performs **next-session preparation
first** so the time-sensitive same-day Schwab snapshot is not delayed by
current-session analysis. It then verifies today's production run coverage
and replays today's journal. No second password prompt is introduced.

For an illustrative November 3-to-November 4 closeout, run it in an
**isolated test checkout only until production installation is approved**:

```cmd
python probes\run_daily_closeout.py --closed-session 2026-11-03 --next-session 2026-11-04
```

That command produces separate, explicit outcomes and a fresh, immutable
dated JSON receipt:

- `NEXT SESSION PREPARATION: READY / INCOMPLETE`.
- `CURRENT SESSION AUDIT: PASS / FAIL / ERROR`. Uni and Focus must both
  have actual production polling runs and the expected 780/1,560 slots.
  An empty opening journal cannot qualify as completed polling.
- `CURRENT SESSION REPLAY: COMPLETE / ERROR`.
- `CLOSEOUT: VERIFIED_COMPLETE` (exit 0), `RECORDED_WITH_FINDINGS`
  (exit 1, for a *real* coverage FAIL), or `INCOMPLETE` (exit 2).

The audit preserves all missing slots and request errors. A replay success
cannot change a failed audit verdict. The independent Health monitor must
still be stopped with **Q**, with its shutdown receipt checked separately.

**This wrapper is not a timer or scheduler.** To prevent another missed
closeout, deployment must also supply an attended after-close launch/reminder,
with explicit owner acknowledgement while the previous session's volume
remains valid. Silent credential storage and unattended password bypass are
out of scope. No scheduled action is installed by this experiment.

## Safely qualify the unified command against retained production evidence

The combined runner now supports `--verify-only`. This mode uses the
**experimental** scripts to inspect the explicitly selected *production* data
root. It does **not** invoke the live post-close builder, does not request
credentials, does not publish an r0 and does not change production journals.
Its dated JSON receipt is stored in the experimental checkout by default.

Example from the isolated MasterBot checkout (run on any later date):

```cmd
python -B "C:\\Users\\danla\\closeout_reliability_20261103\\mb_market_data\\probes\\run_daily_closeout.py" --verify-only --closed-session 2026-11-03 --next-session 2026-11-04 --market-data-root "C:\\Users\\danla\\Documents\\github\\mb_market_data"
```

**Expected qualified result for November 3:**
`CURRENT SESSION AUDIT: FAIL` (one real Focus gap),
`CURRENT SESSION REPLAY: COMPLETE`,
`NEXT SESSION PREPARATION: READY`,
`CLOSEOUT: RECORDED_WITH_FINDINGS`; exit code **1** is intentional, not
an execution error.

In **attended** mode, the program checkout and data root must be the same,
preventing accidental experimental code execution *against* production.
Only after explicit installation approval would a single command in the
installed checkout execute the dated snapshot/build/publish plus audit/replay.

The existing Windows test harness (`probes/verify_closeout_offline.cmd`)
must pass after every candidate change. It never calls a provider.

## New attended closeout entrypoint (review candidate)

The new `probes/prepare_next_session_closeout.py` invokes the **existing**
production `run_daily_universe_production.py --publish-journal-root` five-stage
workflow: Nasdaq directory, **one interactive Schwab password prompt** for the
post-close quote snapshot, deterministic Universe selection, opening r0
proposal, and r0 publication in the next-session schema-v2 journal.
It then verifies the saved artifacts, dates, proposal hash, publication and
replay. The wrapper does not ask for a second password.

It **refuses to start a new build outside 16:00–24:00 ET on the completed
session date** (but can verify an already existing complete build later) and reports `INCOMPLETE` without launching a provider request.
An existing workflow directory is never overwritten or silently regenerated.
If it already exists, the wrapper verifies it without invoking the builder; a
partial or damaged directory requires an explicit review/recovery instead of
silent retry. It does not declare today's journal audit PASS.

Intended command on the **completed session date**, after 16:00 ET:

```cmd
python probes\prepare_next_session_closeout.py --closed-session 2026-11-03 --next-session 2026-11-04
```

Put that command **last in a multi-command CMD block**, because the builder
prompts for a password. This candidate is not installed or Windows-qualified.
The combined two-verdict runner is now committed in the experiment.
A separately reviewed scheduled after-close reminder or attended launcher
is still required so the operator cannot overlook the handoff.

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

## Simplified operator command: one date confirmation, one authentication

The experimental `probes/attended_closeout_today.py` reduces daily
operator entry to one command in the approved production checkout:

```cmd
python probes\attended_closeout_today.py
```

It detects today's **Eastern trading date**, requires an existing dated
session journal and a time after 16:00 ET, asks for the **actual next trading
date** (for example Monday after Friday, or Tuesday after a holiday),
and asks for explicit `YES` approval. It then invokes the combined
closeout. The accepted dated workflow prompts for the encrypted Schwab
password **once**. Audit and replay occur afterward without another
authentication. The user still stops the independent monitor/watchdog
with Q after validating the evidence.

**This is not yet deployed.** There is no exchange calendar in the current
production workflow; the operator must choose the real next session date
and must not rely on weekday arithmetic alone.

## Post-close operator reminder (experimental; not scheduled)

A read-only reminder probe now exists at
`probes/closeout_readiness_reminder.py`. It uses **Eastern time** to avoid
hard-coding MasterBot's local timezone or DST shifts. A completed-session
journal dated today is required; it does not infer a live trading session
from a weekday alone. After 16:10 ET, if no successfully verified next-session
build/r0 is found, it reports `CLOSEOUT REMINDER: DUE`. Once all accepted
artifacts and the next-session r0 agree, it reports `READY` and stops
requesting attention. The probe does not infer the next session by simply
adding one weekday: the operator must check the exchange holiday calendar
and supply `--next-session` to the attended command.

For an isolated checkout, read-only status is checked with:

```cmd
python -B probes\closeout_readiness_reminder.py
```

The optional `--notify` uses a **topmost Windows reminder window** only when
`DUE`, performs no API call, and is guarded against redirecting an
experimental checkout's notifier to production. It does **not** run the
closeout automatically: the operator remains responsible for authenticating
once and confirming the target trading date. It also does not install a
Task Scheduler job. A safe read-only historical check uses an offset-aware
`--as-of` value, for example:

```cmd
python probes\closeout_readiness_reminder.py --as-of 2026-11-03T17:30:00-05:00 --market-data-root C:\Users\danla\Documents\github\mb_market_data
```

The `--as-of` option cannot be combined with `--notify`, preventing a
synthetic historical timestamp from creating a desktop warning.

The **plan-only** Task Scheduler helper is
`probes/install_closeout_reminder_task.ps1`. Without `-Install`, it
prints a plan and changes no Windows tasks. On explicit later approval,
`-Install` requires an installed **main** checkout, rejects an existing
task, and requires the operator to type a full confirmation before registering
an hourly, logged-on-user-only interactive task. The scheduled action
runs `pythonw.exe` to invoke the read-only reminder only: it never
starts Schwab or handles credentials.

Proposed scheduled use **after separate approval**: an interactive,
hourly Windows Task Scheduler invocation of the installed production
checkout's reminder. The script's ET date/time and journal checks determine
whether to pop up; task scheduling must not depend on Central/Arizona local
DST. Only use "Run only when user is logged on" so warnings appear on the
operator desktop. A dismissed reminder may recur hourly until readiness
is proven. If the machine is shut down, signed out or the task does not
run, the reminder cannot guarantee delivery. Manual closeout remains
mandatory. Task creation, operational interruption testing and approval
are still pending.

## Missed-window recovery and optional phone alerts

A missed source-date snapshot is not recreated using the following day's
volume. See `docs/runbooks/Missed_Post_Close_Snapshot_Recovery.md` for
triage, retained-snapshot continuation, and the explicit, separately
authorized prior-roster carry-forward procedure. The experiment now has
read-only `probes/assess_closeout_recovery.py`, which reports
`RECOVERY_REQUIRED` after the source date when readiness cannot be proven.

The proposed optional Pushover interface is described in
`docs/runbooks/Optional_Pushover_Closeout_Notifications.md`.
The `PushoverStub` is integrated with the desktop reminder but is
**disabled and incapable of delivery**, even if an enabled flag is set.
No Pushover account, encrypted credential loader or network transport
is present in this candidate. Earlier scanner work used an encrypted
`pushover.ecfg` with separate app and user keys; that design can be
reviewed when the user reinstates the account.

An official exchange-calendar schedule is **still pending**. In the
current experimental reminder, the hourly Windows Task Scheduler job
could execute all day while logged on, but only the fixed 16:10-to-midnight
Eastern gate can raise a warning; it does not yet support 13:00-close
sessions. The snapshot validator also still uses 16:00 ET. Do not
install the reminder as if early-close support were qualified.

## Remaining deployment work

This is an **experimental closeout candidate, not a production installation**.
Before adoption:
- Execute and review the committed offline tests for missing build, corrupt hash,
  incorrect date, absent r0, hash mismatch, valid complete fixture, and attended
  runner failure/retry behavior.
- Verify the committed dated two-verdict runner against real retained
  November 3/4 evidence in read-only mode, with the original Focus FAIL retained.
- Implement timely post-close prompting/scheduling with credential handling.
  The interactive Schwab password makes silent unattended execution unsafe:
  an alarm or attended runner may be necessary, not an implicit vault bypass.
- Windows-qualify the exact candidate; review and explicitly approve
  production installation, the operating schedule and any credential flow.
- Retain the original November 2 omission and November 3 Focus FAIL.

Until deployment, the operator must explicitly run preparation and verify
tomorrow's r0 as part of the ordinary closeout, as was done for November 4.
