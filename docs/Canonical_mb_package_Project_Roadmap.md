# Canonical mb_package Project Roadmap and Decision Register

**Canonical status date:** 2026-09-16

**Scope:** The entire `mb_package` family of projects and their operating environment

**Role of this document:** Durable roadmap, status register, and decision register

This is the authoritative high-level project record. Weekly chats—named `week YYYY-MM-DD`—perform detailed planning, implementation, testing, and evidence review. Durable conclusions from those chats should be incorporated here periodically. A weekly plan does not change canonical architecture, status, or priorities until its durable conclusion is promoted into this roadmap.

Status terms used here:

- **Completed and validated:** Implemented and supported by recorded test or live-run evidence.
- **Demonstrated / partial:** A proof of concept works, but an identified validation or production-hardening gap remains.
- **Pending:** Accepted work that has not yet been completed.
- **Deferred:** Intentionally postponed until a stated prerequisite or higher-priority milestone is complete.
- **Tabled:** Deliberately removed from active planning; do not revive without a new reason.
- **Superseded / rejected:** Replaced by a later architectural decision; do not silently reintroduce.

## 1. Project objectives and architecture

### 1.1 Primary objective

Build a reliable, explainable, replayable market-observation and watchlist-management system that can:

1. Construct a stable daily stock universe from completed-session data.
2. Observe that universe independently of ThinkOrSwim (ToS).
3. Promote smaller nested sets for closer attention: **Hot ⊆ Focus ⊆ Uni**.
4. Preserve raw observations, membership revisions, timing, and acquisition history in a daily journal.
5. Drive a dashboard from either live data or historical replay.
6. Feed explainable intraday and overnight-volume signals into watchlist decisions.
7. Publish selected membership to ToS as an execution/display adapter, while detecting drift and uncertain GUI outcomes.
8. Eventually support reproducible day replay, historical back-testing, algorithm optimization, and an independent algorithmic-trading component.

The immediate project is a market-data and control foundation. Live algorithmic trading is not an immediate objective.

### 1.2 System boundary and component ownership

| Component | Primary ownership | Canonical role |
| --- | --- | --- |
| `mb_market_data` | MasterBot | Schwab quote/history acquisition; Nasdaq halt acquisition; Uni/Focus/Hot polling; daily SQLite observation journal; replay; dashboard; future historical Overnight Volume (OV), signals, universe selection, and data-quality work. |
| `mb_watchlist_coordinator` | MasterBot | Producer intents, canonical watchlist revisions, precedence, reconciliation, publication transactions, verification, recovery, and adapter health. |
| `schwab_watchlists` | MasterBot | Existing Schwab-based watchlist prototypes and submission workflow; retained while capabilities migrate to their long-term owners. |
| `mb_tools` | Shared, primarily MasterBot | Shared configuration and CLI utilities, secure Schwab configuration, scanner command/status interfaces, window/config tools, and reusable support functions. |
| `ToS_scanner` | El-Cheapo | ToS GUI automation, scans, exports, Watchlist mutation, and scanner-side lifecycle. ToS is an output/execution adapter—not the authoritative market-data plane. |
| `sync_csv_v2` in `thousand_miles\toolkit\synccsv` | Transport layer | Live CSV/file transport. It remains separate from the future after-market archive utility. |

### 1.3 Data-plane hierarchy

| Set | Purpose | Current direction |
| --- | --- | --- |
| **Uni** | Broad daily eligible universe | Built deterministically after the completed regular session; polled independently through the API. Current reference scale is about 759 symbols. |
| **Focus** | Smaller working set requiring more frequent attention and possible ToS publication | Polling has been implemented and journaled. Anticipated steady-state size is roughly 30–50, although current tests used four symbols. |
| **Hot** | Highest-priority, higher-frequency subset | Architectural requirement accepted; polling frequency, admission, and eviction policy remain pending. |

Every membership update must preserve **Hot ⊆ Focus ⊆ Uni**. Membership changes must be deterministic, atomic, revisioned, journaled, and replayable. No observer may see a partially applied hierarchy.

The September 11, September 14, and September 15 test collections predate hierarchy enforcement and use independent Uni and Focus sampling channels. Historical diagnostic replay must reconstruct these legacy journals faithfully and label their hierarchy status; strict nesting validation applies only after a journal declares the hierarchy contract.

### 1.4 Control and publication model

- Producers express intent; they do not directly manipulate ToS.
- The coordinator owns canonical membership and publication decisions.
- OV is a baseline-set producer; Nasdaq LUDP/M is an ensure-present/add producer. Manual overrides have higher precedence where defined.
- `accepted` means an operation was accepted for processing; it does not mean the desired state was satisfied.
- Full satisfaction requires observed membership to equal the desired target.
- An uncertain GUI result remains **unknown** until reconciled; it must not be treated as success.
- Network transport and GUI automation are separate failure domains.
- Restart recovery should begin from the last confirmed state plus any in-flight transaction, not merely from the last command issued.

### 1.5 Long-range analytical architecture

The journal and stable universe are intended to feed an explicit **Intraday Signals** layer. That layer will combine current polling with historical features. Day replay must exercise the same decision path as live operation wherever practical. Back-testing and later algorithm optimization must use timestamped universe membership and stable instrument identity to avoid survivorship bias.

## 2. Completed and validated capabilities

### 2.1 Shared tools and configuration

- `mb_tools` provides the shared configuration model and the operational CLIs used across machines, including `mb-scan-command`, `mb-scan-status`, `mb-schwab-auth`, `mb-env-report`, window/widget survey tools, and the encrypted-config editor.
- Project `.env`, Windows `MB_*`, and packaged defaults have an established precedence model.
- `mb_tools` v0.5.0 was released and installed on MasterBot on 2026-07-31.
- The `export_wl` change was committed and pushed as `f436c2f`; 9 targeted and 59 full tests passed.

### 2.2 El-Cheapo scanner command channel and ToS adapter

- The live file-command lifecycle—`start`, `pause`, `resume`, and `stop`—was validated through the MasterBot-to-El-Cheapo command directory.
- Incoming, accepted, processed, and rejected command states are implemented, with heartbeat/status reporting.
- ToS Watchlist export is implemented and has produced valid timestamped CSV output.
- `replace_wl_symbols` and `add_wl_symbols` were demonstrated after correcting the GUI sequence to Paste → Replace/Add → Save.
- Watchlist-versus-scan behavior in the ToS Watchlist window is understood: a static Watchlist exposes `Import...`; a scan exposes `Edit ...` and is not suitable for static imported membership.
- A critical GUI collision was identified: the JTM Scan Manager can cover the ToS `Symbols Import` dialog. This remains an operational hazard even when the scripted sequence itself is correct.

### 2.3 Nasdaq halt acquisition

- Nasdaq LUDP/M halt-feed parsing and monitoring exist in `mb_market_data`.
- Recorded probes parsed historical and current feed data, and repeated polling distinguished the initial baseline from subsequent new events.
- The feed is available as a producer input to the coordinator.

### 2.4 Coordinator proof of concept

- The coordinator models producer intents, canonical/observed/confirmed state, revisions, pending reconciliation, and publication transactions.
- OV baseline and LUDP/M add semantics have been modeled together.
- Tests increased from 5 to 12 passing in the initial coordinator work.
- OV-driven publication has been demonstrated live.

This is **not** equivalent to completing the entire combined live proof of concept. The remaining combined milestone is a genuinely new LUDP/M event applied on top of the OV-derived Watchlist through the coordinator, followed by full-target verification.

### 2.5 Independent Uni and Focus acquisition

- Uni and Focus run as independent long-running polling processes.
- Focus polling has run at seconds `:05`, `:20`, `:35`, and `:50`.
- Uni polling has run at seconds `:00` and `:30`.
- A 759-symbol Uni request has completed in roughly 0.8–0.9 seconds using two Schwab requests. Recorded runs distinguished valid quotes, invalid symbols, missing responses, and request errors.
- Polling produces console summaries, timestamped CSV output, and optional daily SQLite journal output.
- Scheduler behavior has test coverage for exact-slot-once execution, independent Uni/Focus slots, overrun skipping, end-slot exclusion, membership/session/revision binding, empty-membership rejection, and symbol normalization/deduplication.

### 2.6 Daily SQLite observation journal

- The journal records runs, membership revisions/members, acquisitions, and observations.
- Uni and Focus have both been connected to the journal.
- Timing evidence showed normal Focus acquisition duration around 0.38–0.46 seconds, small journal-write overhead, and generally good quote freshness, with isolated outliers observable rather than hidden.
- **Corrected September 14 evidence:** **2,342 replay events** consisting of 2 membership revisions and 2,340 acquisitions, with **598,260 observations**.
- **Latest canonical evidence:** the September 15 journal passed the same full-day accounting with 2,342 replay events, 2 revisions, 2,340 acquisitions, and 598,260 observations. Uni contributed 780 acquisitions/592,020 observations; Focus contributed 1,560 acquisitions/6,240 observations.
- The September 15 timestamp audit confirmed that all revision and acquisition control timestamps are non-null, normalized 27-character UTC text with microsecond precision. Acquisitions become replay-visible at `completed_at_utc`; scheduled and dispatched times remain provenance.

### 2.7 Dashboard lifecycle and replay

- Dashboard replay completed successfully.
- Both supported server-stopping methods have been confirmed working.
- Therefore, clean server termination is **completed**, not pending.
- Single-day seek-to-historical-time is implemented in commit `f237c5f` and validated through automated tests and manual replay against the September 14 and September 15 journals.
- Exact whole-second input, pre-first-acquisition state, between-acquisition state, backward seek, noon seek, and end-of-data behavior were confirmed. Journal/date selection remains external to the dashboard.

## 3. Current critical path

The critical path now moves from validated acquisition and exact single-day navigation toward live membership evolution and deterministic daily universe construction.

### CP-1 — Seek to historical time

**Status: Completed and validated.**

Commit `f237c5f` adds deterministic single-day seek to the quote dashboard. The implementation reconstructs state from the journal timeline, uses the approved whole-second boundary semantics, pauses after seek, and supports forward and backward seeks without changing the selected journal date.

Evidence includes 15 focused dashboard tests, 143 full tests, and manual demonstrations against the complete September 14 and September 15 journals. Both journals reached 100% at 2,342 replay events and 598,260 observations; pre-acquisition, first-acquisition, noon, backward, and end-of-data targets behaved as specified. The September 11 journal remains standing compatibility evidence for future replay/schema changes.

### CP-2 — Dynamic, atomic membership changes

**Status: Pending.**

Allow membership to change while polling and dashboard processes are running. The implementation must preserve **Hot ⊆ Focus ⊆ Uni**, never expose partial updates, and bind each acquisition to one unambiguous membership revision.

Completion requires:

- an explicit transaction/revision model for hierarchical changes;
- validation before commit;
- atomic publication to readers;
- deterministic normalization and deduplication;
- rejection of changes that would violate nesting;
- durable journal evidence sufficient for exact replay;
- concurrency, restart, and failure-injection tests.

The approved design is recorded in **Atomic Hierarchical Membership Contract and Implementation Plan** (2026-09-16). It selects complete hierarchy snapshots, one global per-session revision, pre-commit nesting validation, atomic SQLite publication, schema-v1 compatibility, and a single bundled replay event for future hierarchy-governed journals. Implementation remains pending.

### CP-3 — Deterministic daily universe selector

**Status: Important near-term work; pending.**

Build the next trading day's stable Uni set after the current regular session has completed.

Established design decisions:

- Use the just-completed regular-session **close**, not current intraday or extended-hours Last.
- Use the completed day's total volume for the initial `Volume >= 10,000` filter.
- If market capitalization is calculated locally, use the same completed-session close with the selected shares-outstanding source.
- Build after the regular-session close so the next trading day's universe is ready before premarket operation.
- Keep version 1 CLI/config driven and operational.
- Preserve deterministic inputs, normalized symbols, explainable rejection reasons, reproducible output, timestamped run records, and explicit dry-run/submit behavior where submission applies.
- The optional GUI is deferred and must not delay the selector.

The selector may first produce a startup-stable Uni snapshot; it does not need to wait for intraday dynamic membership support. Dynamic membership is nevertheless required before later intraday Uni/Focus/Hot changes can be applied safely.

## 4. Near-term work

Near-term work should be handled in small, testable weekly increments—normally one or two implementable steps per weekly plan.

1. **Implement Slice A of the atomic-membership plan.** Begin with the pure `SamplingHierarchyRevision` contract and tests, then add schema-v2 atomic persistence, bundled replay, projector, and audit support.
2. **Implement the deterministic daily universe-selector MVP.** Separate retrieval, conversion, filtering, output, and optional submission. Record input-date/session provenance and filter counts.
3. **Use September 11, September 14, and September 15 as standing real-day regression evidence.** Future replay or schema work should preserve the recorded per-day accounting and sequence evidence where the underlying journal is unchanged.
4. **Validate dynamic membership with live-like concurrent Uni and Focus polling.** Add Hot only after the hierarchy mechanism is correct; do not let Hot design broaden the first atomic-update milestone.
5. **Close the combined coordinator POC milestone when a genuine new LUDP/M event is available.** Verify the complete desired Watchlist, not merely command acceptance or a GUI action.
6. **Document `sync_csv_v2` operationally.** Expand its minimal README to cover the production launcher/stop workflow, transport semantics, testing, and separation from the future after-market archive utility.

## 5. Medium- and long-term work

### 5.1 Medium-term market-data and signal work

- Add HotWatchlist as a higher-frequency subset after atomic hierarchy changes are proven. Decide cadence, capacity, admission, eviction, and back-pressure policy from measurement.
- Build the Intraday Signals layer using current Uni/Focus/Hot observations plus historical features.
- Replace ToS-supplied `OV_DECISION` with MasterBot-computed historical OV analytics.
- Maintain a historical OV store and compute 3/5/10/30-day medians and maxima, relative/unusual-volume measures, near-open volume, price versus previous close, and persistence metrics.
- Add acquisition-time plots, per-symbol extraction, data-quality summaries, and operational health reporting where they aid validation.
- Establish daily data distillation and retention rules so full journals remain auditable while longer historical storage remains manageable.
- Support short-period backtesting across multiple locally collected daily journals without adding seek as a backtesting feature.
- Plan long-term backtesting around provider-sourced historical data—such as Schwab or Massive—rather than waiting years to accumulate local collections. Record source, granularity, and fidelity limitations explicitly.
- Preserve stable instrument identity separately from ticker symbols; record symbol changes, corporate actions, delistings, and dated universe snapshots.

### 5.2 Medium-term coordinator and scanner hardening

- Complete restart recovery from last confirmed state plus in-flight transactions.
- Improve independent liveness checks for all El-Cheapo scanner processes; `mb-scan-status` alone has not historically proven that every required process is alive.
- Define source priority, capacity, eviction, and manual-override policy beyond the intentionally simple POC rules.
- Strengthen full-target verification and drift reconciliation.
- Improve GUI-collision prevention and adapter-health reporting.
- Consider merging `scan_command_loop.py` and `scan_main_v2p0dev0.py` only after the coordinator concept and current scanner behavior are stable. Until then, keep them separate.

### 5.3 Back-testing and trading

- Keep single-day collection replay as an investigative/diagnostic tool with seek; do not treat it as the long-term backtesting interface.
- For short-period studies, evaluate multiple locally collected daily journals through a separate backtesting workflow without seek.
- For long-term studies, fetch historical data from providers such as Schwab or Massive and identify differences from locally collected polling evidence.
- Build historical back-testing on timestamped universe membership and instrument identity where those inputs exist, avoiding survivorship and look-ahead bias.
- Compare full-journal short-period back-testing with back-testing from distilled daily files before choosing the long-term local-storage/query architecture.
- Add algorithm evaluation and optimization against historical data only after the replay and data-quality foundation is trustworthy.
- An independent algorithmic-trading component may eventually consume the same decision outputs, run in simulation/paper mode, undergo back-testing, and later control live trades. This remains conceptual and must be introduced behind explicit risk, brokerage, order-state, recovery, and audit controls.

## 6. Dependencies and unresolved decisions

| Area | Dependency or unresolved decision | Why it matters |
| --- | --- | --- |
| Atomic membership | Transaction boundary, revision numbering, reader handoff, rollback/rejection behavior | Required to preserve Hot ⊆ Focus ⊆ Uni and make every acquisition replayable. |
| Uni selector | Canonical symbol-directory source and availability; price/market-cap bounds; shares-outstanding source; handling of missing/stale fields | Determines daily membership and reproducibility. |
| Trading calendar | Holiday, early-close, and next-trading-day logic | Required to interpret “completed regular session” correctly. |
| Hot | Poll frequency, size, admission/eviction, promotion/demotion, API budget | Must be evidence-driven and cannot break Focus/Uni schedules. |
| Focus publication | Relationship between API Focus, canonical coordinator membership, and ToS display membership | Prevents ToS from becoming an accidental source of truth again. |
| Journal retention | Duration for full SQLite journals; distillation format; archive and deletion policy | Current full-day volume is substantial; auditability must survive compaction. |
| Data quality | Treatment of invalid symbols, partial batches, stale timestamps, and API errors | A quote response is not automatically a valid observation. |
| Coordinator POC | Availability of a genuinely new LUDP/M event for the combined live test | Final proof requires a real transition, not replaying a baseline as “new.” |
| Scanner health | Independent proof that required El-Cheapo processes are alive and the GUI is unobstructed | Heartbeat/command acceptance alone cannot prove adapter readiness. |
| Transport/archive | Exact boundary and handoff between `sync_csv_v2` live transport and future after-market archival | Avoids mixing real-time reliability concerns with long-term data management. |

## 7. Deferred or tabled work

### Tabled

- **Multi-symbol dashboard filtering:** tabled. It is not part of the current dashboard critical path. Revisit only if a concrete analysis or operational need changes the cost/benefit.

### Deferred

- **Dashboard print refinement:** deferred until higher-value replay and membership work is complete.
- **Dashboard date selector:** deferred. Initial seek uses the journal/date selected outside the dashboard; a later selector may choose one daily journal but will not create a multi-day seek or backtesting interface.
- **Universe-selector GUI:** deferred. The first selector remains CLI/config driven.
- **Scanner-process merger:** deferred until behavior, coordinator ownership, and recovery semantics are stable.
- **Full production trading/back-testing platform:** deferred behind data quality, replay fidelity, historical coverage, and risk controls.
- **Separate strategy/back-testing repository:** previously postponed; keep market acquisition/history and its foundational replay work together until a real ownership boundary emerges.
- **Complex source-priority and Watchlist-size policy:** deferred beyond the coordinator POC's intentionally simple rules.

### Superseded or rejected directions

- **Using the ToS Watchlist as the primary market-data universe:** superseded. ToS remains a display/execution adapter; Uni acquisition is API-based and independent.
- **Allowing producers to mutate ToS directly:** rejected by the coordinator architecture. Producers submit intent; the coordinator owns the target and the adapter performs the GUI work.
- **Treating accepted commands as proof of desired state:** rejected. Satisfaction requires observation/reconciliation.
- **Using intraday or extended-hours Last for the next day's deterministic price filter:** rejected. Use the completed regular-session close.
- **Building the selector GUI before the selector works operationally:** rejected as sequencing; the GUI remains optional and deferred.

A previously deferred capability may move forward only when its prerequisite has changed. For example, persistent journaling and replay were once deliberately postponed during early OV probing; they became active work after independent Uni/Focus acquisition was proven and durable evidence was needed. That changed evidence—not a silent reversal—justified the move.

## 8. Operational procedures and validation evidence

### 8.1 Daily operating model

1. After the regular session closes, construct the next trading day's stable Uni snapshot from completed-session close and volume data.
2. Before the next session, validate configuration, credentials, output paths, symbol counts, revision identity, and scanner readiness where ToS publication is expected.
3. Run Uni and Focus as separate long-running processes. Current established slots are Uni at `:00`/`:30` and Focus at `:05`/`:20`/`:35`/`:50`.
4. Write acquisitions, observations, and membership revisions into the dated SQLite journal.
5. Treat invalid symbols, missing quotes, API errors, overruns, and stale timestamps as explicit evidence categories.
6. Use the dashboard against the selected daily journal. Both supported server-stop methods and single-day seek-to-historical-time are validated; journal/date selection remains external.
7. Publish to ToS only through the coordinator/adapter path. Inspect observed membership after mutations; command acceptance is not final verification.
8. Preserve logs, run metadata, journal, and relevant CSV evidence before any after-market distillation or archival step.

### 8.2 Established scanner command pattern

- MasterBot publishes scanner commands to the El-Cheapo `SCANCTRL` share.
- The command lifecycle separates incoming, accepted, processed, and rejected states.
- Use `mb-scan-status` as one health input, not as sole proof that all required processes and GUI conditions are healthy.
- Suspend/resume export operations deliberately when isolating Watchlist mutation or troubleshooting GUI behavior.
- Before live mutation, make sure the ToS pane is a static Watchlist rather than a scan and ensure no manager window is covering the import dialog.

### 8.3 Key validation evidence register

| Date | Capability | Evidence |
| --- | --- | --- |
| 2026-07-20 | Watchlist export support | `mb_tools` change `f436c2f`; 9 targeted and 59 full tests passed. |
| 2026-07-20 onward | File-command lifecycle | Live `start → pause → resume → stop` sequence and network result handling validated. |
| 2026-07-31 | Watchlist mutation | Corrected Paste → Replace/Add → Save sequence demonstrated; later `add_wl_symbols` was accepted and applied. |
| 2026-08-13/14 | Nasdaq halt source | Historical/current parsing and 60-second monitoring demonstrated; initial baseline separated from subsequent new events. |
| 2026-08-30 onward | Broad-universe quote capacity | Approximately 759 symbols acquired in two requests in under one second in recorded probes; invalid members were enumerated. |
| 2026-09-10/11 | Uni/Focus journal integration | Independent schedules, revisions, membership, acquisitions, observations, timing, and replay were exercised; scheduler and journal tests passed. |
| 2026-09-14 | Full journal audit | Corrected audit: **2,342 replay events** = 2 membership revisions + 2,340 acquisitions, with **598,260 observations**. |
| 2026-09-14 | Dashboard replay | Historical dashboard replay completed successfully. |
| 2026-09-14 | Server termination | Both supported server-stopping methods confirmed working; clean termination closed. |
| 2026-09-15 | Full journal audit | 2,342 replay events = 2 revisions + 2,340 acquisitions; 598,260 observations; Uni 780/592,020 and Focus 1,560/6,240; sequence SHA-256 `000f833971677b68f26d7d53d3f2a8efc3d5a8d196723f123abc46cde50897e9`. |
| 2026-09-15 | Timestamp representation | All revision and acquisition timestamps were non-null fixed-width UTC text with microsecond precision; `completed_at_utc` matches replay acquisition event time. |
| 2026-09-16 | Historical seek | Commit `f237c5f`; 15 focused and 143 full tests passed. Manual September 14/15 demonstrations confirmed exact whole-second boundaries, pre-acquisition state, between-acquisition state, noon seek, backward seek, and end-of-data totals. |
| 2026-09-16 | Atomic membership design | Complete hierarchy snapshots, one global revision, atomic schema-v2 publication, bundled replay, schema-v1 compatibility, dynamic poller handoff, and staged acceptance gates recorded; implementation not started. |

### 8.4 Evidence standard

A capability should be marked complete only when the record identifies:

- the exact input/session or fixture;
- the command or procedure used;
- expected and actual counts or state;
- relevant timing and error categories;
- test results or live-observation evidence;
- the commit/version when the evidence depends on code state.

A GUI click, an accepted command, or a process exit code alone is insufficient where downstream state can diverge.

## Canonical decision register

| ID | Decision | Status |
| --- | --- | --- |
| D-001 | ToS is an output/execution adapter, not the primary market-data plane. | Active |
| D-002 | Maintain independent API-based Uni and smaller Focus/Hot sets. | Active |
| D-003 | Preserve **Hot ⊆ Focus ⊆ Uni** through complete, atomically published hierarchy revisions. Invalid nesting is rejected before commit. | Active; design approved, implementation pending |
| D-004 | Use completed regular-session close and completed-day volume to build the next trading day's universe. | Active |
| D-005 | Keep universe-selector v1 CLI/config driven; GUI is deferred. | Active |
| D-006 | Producers express intent; the coordinator owns canonical state and publication. | Active |
| D-007 | Accepted is not satisfied; verify the full observed target and preserve unknown outcomes. | Active |
| D-008 | Daily journals are the audit/replay basis; longer-term distillation must preserve reproducibility. | Active |
| D-009 | Keep `scan_command_loop.py` and `scan_main_v2p0dev0.py` separate during current work. | Active |
| D-010 | Keep live `sync_csv_v2` transport separate from the future after-market archive utility. | Active |
| D-011 | Multi-symbol dashboard filtering is tabled. | Tabled |
| D-012 | Dashboard print refinement is deferred. | Deferred |
| D-013 | Seek-to-historical-time is a completed single-day dashboard diagnostic capability; journal/date selection remains external. | Completed |
| D-014 | Clean dashboard server termination is complete; both supported methods are validated. | Completed |
| D-015 | The recurring planning task is named **Weekly mb_package Project Review**. | Active |
| D-016 | Historical seek is a single-day diagnostic feature. Journal/date selection remains external initially; a dashboard date selector is optional later. | Active |
| D-017 | Short-period backtesting may use multiple local daily journals without seek; long-term backtesting should use provider-sourced historical data rather than waiting years for local collection. | Active |
| D-018 | Seek uses normalized UTC timestamps at full stored microsecond precision; membership revisions use `effective_at_utc`, acquisitions use `completed_at_utc`, and whole-second manual input includes the complete displayed second. | Active |
| D-019 | Legacy independent-channel journals replay faithfully with diagnostic hierarchy status; strict **Hot ⊆ Focus ⊆ Uni** validation applies to hierarchy-governed journals. | Active |
| D-020 | A hierarchy revision is a complete Uni/Focus/Hot snapshot with one global per-session revision and one atomic publication boundary; acquisitions bind to that exact revision. | Active |
| D-021 | Removal is represented by omission from the next complete membership snapshot. Timed removal categories are deferred; `FORCE_ABSENT` remains coordinator intent policy. | Active |
| D-022 | Schema-v1 journals remain immutable legacy evidence. Hierarchy-governed journals use schema v2 and replay each hierarchy revision as one bundled event. | Active |
| D-023 | The coordinator's flat `CanonicalWatchlist` and GUI materialization transaction are not the sampling hierarchy. A distinct coordinator-side hierarchy projection will publish to the journal contract. | Active |

## Maintenance protocol

- Weekly planning task: **Weekly mb_package Project Review**.
- Weekly execution chat naming: `week YYYY-MM-DD`.
- Each weekly chat should begin from this roadmap, select only one or two realistic implementation steps, and report evidence and durable decisions separately from exploratory discussion.
- Promote into this roadmap only durable changes: completed validations, changed architecture, accepted decisions, newly identified dependencies, reprioritization, or explicit defer/table/reject decisions.
- Never change a status from pending to completed without evidence.
- Never revive a deferred, tabled, superseded, or rejected direction without recording the new evidence or dependency change that justifies reconsideration.
- Preserve prior decisions in the register; when one changes, mark it superseded and add the replacement rather than silently rewriting history.

## Update log

### 2026-09-14 — Initial canonical consolidation

- Established this roadmap as the durable project and decision register.
- Recorded the then-stated Uni/Focus result as 2,342 acquisitions and 598,260 observations; the acquisition count was corrected on 2026-09-15 to 2,342 replay events containing 2 revisions and 2,340 acquisitions.
- Recorded successful dashboard replay.
- Closed clean server termination after both supported stop methods were confirmed.
- Kept seek-to-historical-time pending.
- Kept dynamic atomic membership pending with **Hot ⊆ Focus ⊆ Uni** as a hard invariant.
- Kept the deterministic daily universe selector as important near-term work.
- Tabled multi-symbol dashboard filtering.
- Deferred dashboard print refinement and the universe-selector GUI.
- Recorded the planning-task name **Weekly mb_package Project Review**.

### 2026-09-15 — Seek specification review and full-day audit

- Corrected September 14 accounting to 2,342 replay events: 2 membership revisions plus 2,340 acquisitions; observations remain 598,260.
- Recorded the audited September 15 journal with identical event/revision/acquisition/observation accounting and its sequence SHA-256.
- Confirmed normalized fixed-width UTC control timestamps with microsecond precision and selected `completed_at_utc` as acquisition replay event time.
- Recorded that current real-day test journals use legacy independent Uni and Focus channels; strict hierarchy validation is reserved for hierarchy-governed journals.
- Defined seek as a single-day diagnostic feature and deferred an optional dashboard date selector.
- Separated short-period local-journal backtesting from long-term provider-sourced backtesting; seek is unsupported in both backtesting workflows.

### 2026-09-16 — Historical seek completion and atomic-membership design

- Marked single-day historical seek completed at commit `f237c5f` after 15 focused tests, 143 full tests, and manual September 14/15 journal demonstrations.
- Confirmed exact whole-second, backward-seek, pre-acquisition, between-acquisition, noon, and end-of-data behavior.
- Approved the atomic hierarchical membership contract: complete normalized Uni/Focus/Hot snapshots, one global per-session revision, reject invalid nesting, publish in one SQLite transaction, and bind every acquisition to its exact revision.
- Preserved completed schema-v1 journals without migration; future hierarchy journals use schema v2 and one bundled membership replay event.
- Kept removal simple as omission from the next snapshot and deferred timed-removal categories.
- Kept the coordinator's flat canonical Watchlist separate from the future sampling-hierarchy projection.
