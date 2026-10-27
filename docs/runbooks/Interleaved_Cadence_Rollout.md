# Interleaved cadence — review candidate, October 27

The candidate adds an explicit `--cadence-profile interleaved-v1` to the existing
schema-v2 pollers. The default is `legacy-v1`. Production installation and the
next-session opt-in require separate operator approval after Windows qualification.

| Channel | Legacy seconds | Interleaved seconds | Full regular-session slots |
| --- | --- | --- | --- |
| Uni | 00, 30 | 00, 30 | 780 |
| Focus | 05, 20, 35, 50 | 10, 20, 40, 50 | 1,560 |
| Future Hot | Disabled | Other even seconds are reserved | 9,360 opportunities |

Hot remains disabled. No Hot poller option, daily Hot driver or new automatic
process startup is added to these production probes by this increment.

## Validation and source deployment

The separately delivered operating-foundation packet contains command 57 for
offline Windows verification, command 58 for a read-only operating review plan,
and command 59 for a read-only deployment review. Qualified source is supplied
in a Git bundle based on production `a082f544112643b4e5f459d15c67f60d0a1ef83d`.

The install operation is prepared for use only after explicit production-change
approval, after 16:00 ET, daily closeout, and stopping Health. It refuses unknown
process inventory, live poller/Health processes, a different HEAD/branch, tracked
edits or untracked files colliding with new source. Unrelated untracked files
are preserved and do not block review. It uses a fast-forward merge, source
hash readback and an installation receipt. The rollback operation is a guarded
Git revert, with its own approval and idle checks; it never uses reset/hard,
force, or journal edits. Existing journals and acquired evidence are retained.

## Fresh-session operation

1. Finish normal post-close universe preparation and opening-r0 verification.
2. On the target morning, complete credential preflight, full-Uni API OV after
   09:00 ET, Focus40/r1 publication, native audit and complete replay as usual.
3. Generate the operating plan from those exact r0/r1 proposals. In the two
   normal independent poller commands, explicitly add
   `--cadence-profile interleaved-v1` to **both Uni and Focus**. Keep the dated
   09:30–16:00 ET window, schema 2, journal root and batch size 400.
4. Start the current source `probes/polling_health.py monitor` with
   `--supervise --cadence-profile interleaved-v1`, after both pollers register.
   Use the generated commands/current source monitor for this session rather
   than an old isolated packet's embedded Health copy.
5. Inspect fresh opening commits and the whole session. At close, obtain the
   full native audit and complete replay with Hot still disabled before any
   later production Hot adoption.

The source checks the session cadence before the password/client/output stage
and repeats it atomically inside native run registration. Competing starts
cannot register different named cadences into one journal. A same-cadence
manual restart retains the original full window and resumes future slots; no
past-slot catch-up is added. Historical runs without a cadence label retain
their recorded legacy interpretation.

## Health, recovery, historical display and rollback

Health can infer a named cadence from saved run metadata, or accept the explicit
profile. A mismatch or unknown schedule is reported as unavailable evidence,
rather than inventing missing slots. Interleaved STATUS output provides complete
current-source restart commands including the correct profile. Duplicate
guards still consider every same-channel/journal process, whatever its profile.
The watchdog passes the profile through to its read-only child.

Audit already derives expected slots from each run's recorded `poll_seconds`.
Replay event serialization is unchanged; a legacy native fixture reproduces
the unchanged a082 replay SHA-256
`10d5c9cc73e6ac503e4006bf5d7aaa4539225f4881ec3f0d0cea32748793b232`.
The historical Overlay now obtains per-run cadence metadata for the endpoint of
an open request-error segment, so a 20-second Focus gap is represented correctly.
Existing unlabelled input retains its legacy display fallback.

Operational rollback is at the **next fresh session boundary**: use the original
commands without `--cadence-profile`, which select legacy defaults on either
source version. Never change a running session's profile or rewrite its run
metadata. Code rollback, when approved, occurs after close with processes idle;
the revert receipt records the resulting commit for the next reviewed update.

Local/packet qualification is synthetic. The real fresh-session cadence run,
Windows acceptance, deployment approval and daily Hot integration are separate
results and must be recorded as such.
