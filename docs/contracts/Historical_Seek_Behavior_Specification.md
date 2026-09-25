# Historical Seek Behavior Specification and Acceptance-Test Matrix

**Project:** `mb_package` / `mb_market_data` dashboard replay  
**Planning week:** 2026-09-14  
**Draft date:** 2026-09-15  
**Status:** Reviewed specification; behavioral decisions approved; no implementation has begun

## 1. Purpose

Define deterministic **seek-to-historical-time** behavior for dashboard replay. A seek must reconstruct the state that was knowable at the requested historical instant. It is not merely a chart-position change.

The specification is grounded in the audited September 14 journal:

- 2,342 acquisitions;
- 598,260 observations;
- successful historical dashboard replay;
- validated clean server termination by both supported methods.

The audited September 14 journal will serve as the primary production-scale regression case. The full-day September 11 Uni/Focus collection will serve as a second real-day regression case. A small synthetic journal will also be required because the collected journals may not contain every membership-transition and timestamp-collision edge case.

## 2. Scope

### In scope

- Seeking within one loaded historical journal/session.
- Selecting the journal/date outside the dashboard for the initial implementation.
- Reconstructing Uni, Focus, and—when present—Hot membership at a target time.
- Reconstructing the latest eligible observation for each active member.
- Seeking forward or backward while replay is paused or running.
- Boundary behavior before the first acquisition, between acquisitions, exactly at an event time, and after the last acquisition.
- Defined replay state following a seek.
- Repeatability, journal integrity, error reporting, and acceptance evidence.

### Out of scope

- Live-mode seeking.
- Multi-day navigation or automatic switching between journal files.
- A dashboard date selector. This may be reconsidered later as a convenience for choosing one single-day journal.
- Short-period or long-term backtesting.
- Dynamic membership implementation itself.
- Multi-symbol dashboard filtering, which remains tabled.
- Dashboard print refinement, which remains deferred.
- New chart types or cosmetic dashboard redesign.
- Journal-schema redesign except where a missing ordering field makes deterministic replay impossible.

### Relationship to replay and backtesting

Historical seek is an investigative and diagnostic feature for one selected daily collection. It is not a general backtesting control.

| Capability | Intended data | Purpose | Seek support |
| --- | --- | --- | --- |
| Single-day collection replay | One locally collected daily journal | Investigation, diagnostics, and visual inspection | Yes |
| Short-period backtesting | Multiple locally collected daily journals | Recent studies using high-fidelity collected observations | No |
| Long-term backtesting | Historical data fetched from Schwab, Massive, or another provider | Months or years of strategy evaluation without waiting to accumulate equivalent local history | No |

Provider-sourced history may have different granularity and will not automatically reproduce the polling times, membership revisions, missing observations, or API behavior contained in locally collected journals. Backtest results must identify their data source and must not imply greater replay fidelity than the source provides.

## 3. Established requirements

These requirements come from the canonical project roadmap and are not new decisions in this document:

1. Replay uses the daily journal as its audit basis.
2. The journal records runs, membership revisions and members, acquisitions, observations, and timing.
3. Every acquisition is bound to one unambiguous membership revision.
4. Reconstructed membership must preserve **Hot ⊆ Focus ⊆ Uni**.
5. The result must be deterministic and replayable.
6. A visual jump without state reconstruction is insufficient.
7. Targets before, between, and after acquisitions require explicit behavior.
8. Pause/resume and replay-speed behavior following a seek must be defined.

## 4. Agreed decisions

These choices resolve the open issues identified during specification review.

| ID | Agreed decision | Reason |
| --- | --- | --- |
| SD-1 | A seek target is an exact discrete timestamp with an **inclusive cutoff**: journal events whose effective timestamp is `<= target` are eligible. | Matches the natural meaning of “state as of this time” and makes exact-boundary tests unambiguous. |
| SD-2 | User-facing seek times are Eastern Time and are normalized to the journal’s canonical internal timestamp representation before comparison. Whole-second manual input means through the end of that displayed second. | Preserves the project’s established ET convention and gives intuitive behavior for acquisitions occurring fractionally after a scheduled polling second. |
| SD-3 | Every seek performs reconstruction from durable journal evidence, not by incrementally undoing or fast-forwarding the dashboard’s current in-memory state. | Forward and backward seeks then produce the same result as a fresh replay at the target. |
| SD-4 | A completed seek always leaves replay **paused at the requested target**. The selected replay-speed setting is retained, but movement resumes only after an explicit Resume action. | Prevents the display from moving away before the user can inspect it and makes running-versus-paused behavior identical. |
| SD-5 | Seeking is available only in historical replay mode. A live dashboard must reject or disable the operation. | Avoids confusing historical reconstruction with the live observation stream. |
| SD-6 | The dashboard must distinguish the requested seek time from the timestamps of the membership revision and observations actually displayed. | Makes stale or not-yet-sampled values visible instead of implying that every value was observed at the seek instant. |
| SD-7 | Timestamp comparison must use a lossless discrete representation, preferably integer microseconds in UTC; floating-point equality is not permitted. Display rounding never controls event eligibility. | Prevents rounding-dependent boundary behavior. The existing journal representation must be inspected before implementation. |
| SD-8 | The initial dashboard operates on one journal selected outside the dashboard. A future date selector may load a different single-day journal, but it does not create a multi-day seek or backtesting interface. | Keeps the current implementation focused while preserving a clean later usability enhancement. |

## 5. Definitions

- **Target time (`T`)**: The normalized, discrete historical cutoff derived from the user’s request.
- **Canonical timestamp**: The lossless internal value used for ordering and comparison. Integer microseconds in UTC are preferred; an existing lossless journal representation may be converted to that form during reconstruction.
- **Exact event time**: The event’s complete stored timestamp at canonical precision, not its rounded dashboard label.
- **Effective membership revision**: The latest committed revision for a set whose effective time is `<= T`.
- **Active member**: A symbol contained in a set’s effective membership revision at `T`.
- **Eligible acquisition**: A completed acquisition with an acquisition time `<= T` and a valid binding to its recorded membership revision.
- **Eligible observation**: An observation belonging to an eligible acquisition during a revision in which that symbol was a member of that acquisition’s set.
- **Displayed observation**: The latest eligible observation for an active member in the relevant polling stream.
- **Source time**: The observation/acquisition time from which a displayed value came.
- **No observation yet**: The symbol is active at `T`, but no eligible observation exists at or before `T`.
- **End of recorded data**: `T` is later than the last completed acquisition available in the loaded journal.

## 6. Behavioral contract

### HS-1 — Target interpretation

1. The manual seek control accepts a date and whole-second time in Eastern Time. Additional precision may be supported, but is not required for the first implementation.
2. Whole-second manual input means through the end of that displayed second. For example, `09:30:05` normalizes to the final canonical tick within the `09:30:05` second.
3. Selection of a specific journal event uses that event’s complete stored canonical timestamp rather than a rounded display value.
4. Timestamp comparison uses a lossless discrete representation. Floating-point timestamps or equality comparisons are not permitted.
5. The target is inclusive: events whose canonical timestamp is exactly equal to `T` are included.
6. The requested target remains visible after the seek, along with the effective normalized cutoff when the distinction matters diagnostically.
7. A malformed, nonexistent, or ambiguous local time must not be guessed. It must be rejected with a specific explanation unless the interface supplies enough offset information to disambiguate it.
8. A target outside the date/session supported by the loaded single-day journal is rejected. The journal/date is initially selected outside the dashboard; a dashboard date selector is an optional later enhancement.

### HS-2 — Atomic reconstruction

A seek produces one complete replacement dashboard state. Readers must not observe an intermediate mixture of old and newly reconstructed state.

Conceptually, reconstruction performs these steps:

1. Establish a consistent read view of the selected journal.
2. Normalize `T` to the journal’s canonical time basis.
3. Select the effective Uni, Focus, and Hot revisions at `T`.
4. Validate the reconstructed hierarchy where those sets exist.
5. Determine active members for each set.
6. For every active member, select the latest eligible observation at or before `T` from the relevant polling stream.
7. Mark active members with no eligible observation as **No observation yet**.
8. Build the complete replacement state, including provenance and status fields.
9. Publish that state atomically to the dashboard.
10. Leave replay paused at `T`.

If deterministic reconstruction or hierarchy validation fails, the seek fails closed: the prior complete dashboard state remains displayed and an error is reported. Partial reconstructed state must not be published.

### HS-3 — Membership reconstruction

1. For each set, use the latest committed membership revision whose effective time is `<= T`.
2. Do not use the current live membership or the last membership loaded into process memory as a substitute.
3. A symbol added at or before `T` is active even if it has not yet received its first observation; show **No observation yet**.
4. A symbol removed at or before `T` is absent, even if an older observation exists.
5. An observation from an earlier revision may be carried forward for a still-active symbol when it remains the latest eligible observation at `T`.
6. The dashboard must retain each displayed observation’s acquisition and revision provenance; it must not relabel an earlier observation as belonging to the current revision.
7. If no membership revision is effective at `T`, that set is **Not yet established** rather than empty-by-assumption.
8. Reconstructed state must satisfy **Hot ⊆ Focus ⊆ Uni**. A violation is evidence of an invalid or incomplete journal and must not be silently repaired during seek.

### HS-4 — Observation selection

1. Observation selection is performed per polling stream/set. Uni and Focus observations must not be silently merged merely because they share a symbol.
2. For an active member, select the most recent eligible observation with acquisition time `<= T`.
3. Failed, rejected, or incomplete acquisitions must not create fabricated observations.
4. A partial acquisition may contribute only observations that were durably recorded and meet the journal’s validity rules; the dashboard must expose the acquisition status.
5. Missing or invalid values remain missing/invalid. Seeking must not forward-fill across an absence unless an earlier valid observation is explicitly displayed with its original source time.
6. Equal canonical timestamps are resolved using a durable journal ordering key. If the journal cannot provide a stable order, the seek fails as nondeterministic.

### HS-5 — Boundary behavior

| Target position | Required behavior |
| --- | --- |
| Before the first acquisition, but after a membership revision exists | Show effective membership with **No observation yet** for its members. |
| Before both the first acquisition and first membership revision | Show sets as **Not yet established** and no observations. |
| At an acquisition’s complete stored timestamp | Include that completed acquisition and its durable observations. |
| Between two acquisitions | Use the latest eligible acquisition/observations at or before `T`; do not interpolate. |
| At a membership revision’s complete stored effective timestamp | Include the committed revision and apply add/remove behavior immediately. |
| After the last acquisition | Show final state as of `T`, label **End of recorded data**, and remain paused. |

### HS-6 — Playback behavior

1. A seek requested while replay is running first stops advancement and reconstructs the requested state.
2. A seek requested while paused uses the same reconstruction path.
3. Both cases finish paused at `T`.
4. The previously selected replay speed is preserved but inactive until Resume.
5. Resume continues from `T`; it must not restart at the previous cursor or at the next wall-clock slot without reference to `T`.
6. Repeated seeks to the same `T` are idempotent.
7. Seeking backward and then forward must match fresh reconstruction at each target.

### HS-7 — Displayed provenance and status

After a successful seek, the dashboard should make these values available to the user or diagnostic output:

- requested target time;
- replay state: Paused;
- data-range status: Before data, Within data, or End of recorded data;
- effective revision identifier and effective time for each set;
- displayed observation’s source/acquisition time;
- observation age relative to `T`;
- acquisition/revision provenance where diagnostic detail is enabled;
- explicit No observation yet, missing, invalid, partial-acquisition, or error state.

## 7. Determinism and integrity rules

1. The same journal plus the same target must produce the same reconstructed state regardless of the dashboard’s prior cursor position.
2. Reconstruction queries are read-only. Seeking must not modify journal counts, revisions, acquisition status, or observations.
3. The journal view used by one seek must be internally consistent. If the file can change concurrently, use an appropriate read transaction or snapshot boundary.
4. Ordering cannot rely on database row-return order without an explicit ordering key.
5. A failed seek leaves the previous complete display intact.
6. Errors must identify the target, journal, and failure category without falsely reporting successful reconstruction.

## 8. Acceptance-test data

### 8.1 Production-scale regression journals

#### September 14 audited baseline

Use a protected copy of the audited September 14 Uni/Focus journal. Before and after the test run, verify:

- acquisitions = 2,342;
- observations = 598,260;
- existing journal audit passes;
- normal full replay still completes successfully.

#### September 11 cross-day regression

Use a protected copy of the full-day September 11 Uni/Focus collection to demonstrate that seek reconstruction is not coupled to the September 14 journal. Before and after testing, verify the journal’s existing integrity evidence and confirm that normal full replay still completes successfully. Do not invent record counts that have not been separately audited.

#### Test reference records

Select actual, durably identified journal records as test reference points rather than inventing timestamps:

- `A_first`: first completed acquisition, identified by its durable key and complete stored timestamp;
- `A_i` and `A_i+1`: two adjacent acquisitions in the same stream, each identified by durable key and complete stored timestamp;
- `A_last`: last completed acquisition, identified by its durable key and complete stored timestamp;
- at least one symbol observed in both Uni and Focus;
- at least one invalid, missing, stale, partial, or error case if present.

### 8.2 Synthetic edge-case journal

Create a minimal deterministic fixture containing:

- Uni, Focus, and Hot revisions that preserve nesting;
- a symbol that remains through a revision change;
- a symbol added before its first observation;
- a symbol absent from a committed membership revision after having an observation in an earlier revision; the policy or cause of removal is outside this specification;
- two durably ordered events sharing the same timestamp;
- a failed acquisition;
- a partial acquisition;
- deliberately invalid hierarchy data for fail-closed testing.

The synthetic fixture tests logic that the collected September 11 and September 14 journals may not exercise. It does not replace either real-day regression journal. Blocking, `FORCE_ABSENT`, temporary suppression, reinstatement, and other removal-policy semantics remain deferred to the dynamic membership transaction design; seek consumes only the resulting committed revisions.

## 9. Acceptance-test matrix

| ID | Data | Scenario | Expected result |
| --- | --- | --- | --- |
| AT-01 | Sept. 14 | Seek to a target just before `A_first` | Effective membership is shown if established; no later observation appears. |
| AT-02 | Sept. 14 | Seek to the complete stored timestamp of `A_first` | `A_first` observations are included; replay is paused at the requested target. |
| AT-03 | Sept. 14 | Seek between `A_i` and `A_i+1` | No event after the target is included; `A_i+1` is excluded. |
| AT-04 | Sept. 14 | Seek to the complete stored timestamp of `A_i+1` | Completed `A_i+1` data is eligible under the inclusive cutoff. |
| AT-05 | Sept. 14 | Seek after `A_last` | Final reconstructed state is shown with End of recorded data; replay remains paused. |
| AT-06 | Sept. 14 | Seek backward from a later target | Result exactly matches a fresh reconstruction at the earlier target. |
| AT-07 | Sept. 14 | Seek forward after AT-06 | Result exactly matches a fresh reconstruction at the later target. |
| AT-08 | Sept. 14 | Repeat the same seek from several prior states | Membership, values, provenance, and status are identical each time. |
| AT-09 | Sept. 14 | Seek while running versus while paused | Both produce the same state and finish paused; selected speed is retained. |
| AT-10 | Sept. 14 | Shared symbol in Uni and Focus | Each stream’s observation and provenance remain distinguishable. |
| AT-11 | Sept. 14 | Run the complete seek suite | Journal remains at 2,342 acquisitions and 598,260 observations; audit still passes. |
| AT-12 | Sept. 11 | Repeat representative before-first, exact-record, between-records, backward, forward, and after-last cases | Results obey the same contract as September 14 and normal full replay remains valid. |
| AT-13 | Either real day | Enter a whole-second target containing an acquisition with fractional seconds | The target normalizes through the end of the displayed second and includes the eligible acquisition. |
| AT-14 | Synthetic | Seek immediately before and at the complete stored time of a membership commit | Earlier revision applies before; the new committed revision applies at its complete stored effective time. |
| AT-15 | Synthetic | Seek after symbol admission but before its first observation | Symbol is active and marked No observation yet. |
| AT-16 | Synthetic | Seek after a committed revision makes a previously observed symbol absent | Symbol and its older observation are absent from that set; seek does not interpret the removal policy. |
| AT-17 | Synthetic | Unchanged symbol spans a revision | Latest eligible older observation may carry forward with original provenance. |
| AT-18 | Synthetic | Two events have the same canonical timestamp | Durable ordering key produces the documented, repeatable final state. |
| AT-19 | Synthetic | Failed acquisition precedes target | No fabricated observation appears; failure status is exposed. |
| AT-20 | Synthetic | Partial acquisition precedes target | Only durably valid recorded observations appear, with partial status exposed. |
| AT-21 | Synthetic | Journal contains Hot/Focus/Uni nesting violation | Seek fails closed; prior dashboard state remains intact; violation is reported. |
| AT-22 | Either | Invalid, nonexistent, ambiguous, or unsupported-date target | Request is rejected with a specific explanation and no display mutation. |
| AT-23 | Either | Resume after a completed seek | Replay continues from the requested target at the retained speed. |

## 10. Completion criteria

Historical seek may be marked completed only when all of the following are true:

1. The agreed decisions in Section 4 are implemented without silently changing their semantics.
2. The implementation uses one deterministic reconstruction path for forward and backward seeks.
3. The full acceptance matrix passes.
4. The September 14 audit remains unchanged at 2,342 acquisitions and 598,260 observations.
5. Representative seek behavior and normal complete replay pass against the full-day September 11 collection.
6. Existing complete replay and both server-stopping methods still work.
7. A recorded demonstration shows before-first, between-acquisitions, complete-timestamp boundary, whole-second input, backward, forward, and after-last behavior.
8. Evidence records the journal identity, commands/procedure, expected and actual results, tests, and relevant commit.

## 11. Implementation-planning handoff and deferred decisions

Specification review approved:

1. inclusive `<= T` cutoff;
2. lossless discrete timestamp comparison with no floating-point equality;
3. Eastern Time manual input, with whole seconds interpreted through the end of the displayed second;
4. reconstruction from durable journal state rather than incremental cursor mutation;
5. always pause after seeking while retaining the selected speed;
6. historical-replay-only availability;
7. explicit requested-time, source-time, revision, and data-status provenance;
8. single-day diagnostic scope with journal/date selection outside the dashboard initially.

The following decisions remain outside this specification:

- the policy representation and lifetime of blocking, `FORCE_ABSENT`, removal, temporary suppression, and reinstatement;
- dynamic atomic membership implementation;
- an optional dashboard date selector for choosing one daily journal;
- short-period backtesting across collected journals;
- long-term backtesting from Schwab, Massive, or other historical sources.

The next implementation-planning step is to inspect the current journal timestamp representation and map each behavioral requirement to the existing replay/dashboard functions and tests, using exact named insertion points.

## 12. Review change record

### 2026-09-15 — Consolidated review

- Defined “exactly” using complete canonical timestamps rather than rounded display values.
- Prohibited floating-point equality and selected lossless discrete timestamp comparison, preferably integer microseconds in UTC.
- Defined whole-second manual input as through the end of the displayed second.
- Replaced “journal landmarks” with “test reference records.”
- Kept the cause and policy of symbol removal outside historical-seek scope.
- Added the full-day September 11 collection as a second real-day regression case.
- Kept initial journal/date selection outside the dashboard and classified a dashboard date selector as an optional later convenience.
- Separated single-day diagnostic replay from short-period collected-journal backtesting and long-term provider-sourced backtesting.
