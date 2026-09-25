# Canonical mb_package Project Roadmap and Decision Register

**Canonical status date:** 2026-09-23

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
7. Optionally publish selected membership outbound to ToS as an unverified
   display adapter; never use ToS readback as market-data evidence.
8. Eventually support reproducible day replay, historical back-testing, algorithm optimization, and an independent algorithmic-trading component.

The immediate project is a market-data and control foundation. Live algorithmic trading is not an immediate objective.

### 1.2 System boundary and component ownership

| Component | Primary ownership | Canonical role |
| --- | --- | --- |
| `mb_market_data` | MasterBot | Schwab quote/history acquisition; Nasdaq halt acquisition; Uni/Focus/Hot polling; daily SQLite observation journal; replay; dashboard; future historical Overnight Volume (OV), signals, universe selection, and data-quality work. |
| `mb_watchlist_coordinator` | MasterBot | Producer intents, canonical watchlist revisions, precedence, reconciliation, publication transactions, verification, recovery, and adapter health. |
| `schwab_watchlists` | MasterBot | API-OV evidence verification, deterministic Focus-r1 production, coordinator intents, and retained legacy watchlist prototypes. |
| `mb_tools` | Shared, primarily MasterBot | Shared configuration and CLI utilities, secure Schwab configuration, scanner command/status interfaces, window/config tools, and reusable support functions. |
| `ToS_scanner` | El-Cheapo | Display-only ToS GUI automation and scanner-side lifecycle. Roster replacement is outbound; scheduled/explicit exports and readback verification are disabled in display-only mode. |
| `sync_csv_v2` in `thousand_miles\toolkit\synccsv` | Transport layer | Live CSV/file transport. It remains separate from the future after-market archive utility. |

### 1.3 Data-plane hierarchy

| Set | Purpose | Current direction |
| --- | --- | --- |
| **Uni** | Broad daily eligible universe | Built deterministically after the completed regular session and polled independently through the API. The first full schema-v2 session used 554 symbols. |
| **Focus** | Smaller working set requiring more frequent attention and possible ToS display | API-OV Focus-r1 production and polling are implemented. The first full schema-v2 session used 40 symbols. |
| **Hot** | Highest-priority, higher-frequency subset | Architectural requirement accepted; polling frequency, admission, and eviction policy remain pending. |

Every membership update must preserve **Hot ⊆ Focus ⊆ Uni**. Membership changes must be deterministic, atomic, revisioned, journaled, and replayable. No observer may see a partially applied hierarchy.

The September 11, September 14, and September 15 test collections predate hierarchy enforcement and use independent Uni and Focus sampling channels. Historical diagnostic replay must reconstruct these legacy journals faithfully and label their hierarchy status; strict nesting validation applies only after a journal declares the hierarchy contract.

### 1.4 Control and publication model

- Producers express intent; they do not directly manipulate ToS.
- The coordinator owns canonical membership and publication decisions.
- OV is a baseline-set producer; Nasdaq LUDP/M is an ensure-present/add producer. Manual overrides have higher precedence where defined.
- For authoritative adapters, `accepted` means an operation was accepted for processing; it does not mean the desired state was satisfied.
- Authoritative-adapter satisfaction requires observed membership to equal the desired target.
- ToS is the deliberate exception: display submission is reported as submitted/unverified and never gates canonical state.
- Network transport and GUI automation are separate failure domains.
- Restart recovery should begin from the last confirmed state plus any in-flight transaction, not merely from the last command issued.

### 1.5 Long-range analytical architecture

The journal and stable universe are intended to feed an explicit **Intraday Signals** layer. That layer will combine current polling with historical features. Day replay must exercise the same decision path as live operation wherever practical. Back-testing and later algorithm optimization must use timestamped universe membership and stable instrument identity to avoid survivorship bias.

## 2. Completed and validated capabilities

### 2.1 Shared tools and configuration

- `mb_tools` provides the shared configuration model and the operational CLIs used across machines, including `mb-scan-command`, `mb-scan-status`, `mb-schwab-auth`, `mb-env-report`, window/widget survey tools, and the encrypted-config editor.
- Project `.env`, Windows `MB_*`, and packaged defaults have an established precedence model.
- The cross-project [Credential Storage and Resolution Contract](https://github.com/DanArizona/mb_tools/blob/main/docs/Credential_Storage_and_Resolution_Contract.md) assigns shared credential mechanics and policy to `mb_tools`. Schwab follows the implemented model; Pushover and Massive entries are design reservations until their loaders, tests, and operating procedures exist.
- `mb_tools` v0.5.0 was released and installed on MasterBot on 2026-07-31.
- The `export_wl` change was committed and pushed as `f436c2f`; 9 targeted and 59 full tests passed.

### 2.2 El-Cheapo scanner command channel and ToS adapter

- The live file-command lifecycle—`start`, `pause`, `resume`, and `stop`—was validated through the MasterBot-to-El-Cheapo command directory.
- Incoming, accepted, processed, and rejected command states are implemented, with heartbeat/status reporting.
- Legacy ToS Watchlist export produced valid timestamped CSV output. In the
  current display-only mode, scheduled and explicit exports remain suspended.
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
- Legacy ToS-derived OV publication was demonstrated live. The first API-only
  OV-to-Focus opening session completed on 2026-09-23 with full-day audit and
  replay evidence. ToS remained optional outbound display only.

This is **not** equivalent to completing the entire combined live proof of
concept. The remaining combined milestone is a genuinely new LUDP/M event
applied on top of API-OV Focus membership through the coordinator, followed by
canonical/journal verification. ToS readback is outside that acceptance path.

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
- **Latest schema-v2 API-only evidence:** the September 23 journal passed audit
  and replay with 2 hierarchy revisions, 2,340 acquisitions, and 494,520
  observations. Uni contributed 780 acquisitions/432,120 observations; Focus
  contributed 1,560 acquisitions/62,400 observations; no slots were skipped or
  missing and no observation-row mismatches occurred. Replay sequence SHA-256
  was `9d8cddf55df11007b6ec6f771b2250d576e6f600d126011046c5525d99c3856a`.
- The September 15 timestamp audit confirmed that all revision and acquisition control timestamps are non-null, normalized 27-character UTC text with microsecond precision. Acquisitions become replay-visible at `completed_at_utc`; scheduled and dispatched times remain provenance.

### 2.7 Dashboard lifecycle and replay

- Dashboard replay completed successfully.
- Both supported server-stopping methods have been confirmed working.
- Therefore, clean server termination is **completed**, not pending.
- Single-day seek-to-historical-time is implemented in commit `f237c5f` and validated through automated tests and manual replay against the September 14 and September 15 journals.
- Exact whole-second input, pre-first-acquisition state, between-acquisition state, backward seek, noon seek, and end-of-data behavior were confirmed. Journal/date selection remains external to the dashboard.

## 3. Current critical path

Two full API-only openings are complete. The daily-universe timing gate and
the authentication usability gaps exposed by the first session are closed.
The critical path now proceeds to the narrowly bounded Observation
Overlay/OOOHLCV diagnostic MVP and then live hierarchical membership
evolution. The 09:00 OV cutoff requires an ordinary next-session live
validation but does not displace those milestones.

### CP-1 — Seek to historical time

**Status: Completed and validated.**

Commit `f237c5f` adds deterministic single-day seek to the quote dashboard. The implementation reconstructs state from the journal timeline, uses the approved whole-second boundary semantics, pauses after seek, and supports forward and backward seeks without changing the selected journal date.

Evidence includes 15 focused dashboard tests, 143 full tests, and manual demonstrations against the complete September 14 and September 15 journals. Both journals reached 100% at 2,342 replay events and 598,260 observations; pre-acquisition, first-acquisition, noon, backward, and end-of-data targets behaved as specified. The September 11 journal remains standing compatibility evidence for future replay/schema changes.

### CP-2 — Dynamic, atomic membership changes

**Status: Core hierarchy path and the first full API-only schema-v2 opening are
validated. A genuine post-opening membership transition remains pending.**

Allow membership to change while polling and dashboard processes are running. The implementation must preserve **Hot ⊆ Focus ⊆ Uni**, never expose partial updates, and bind each acquisition to one unambiguous membership revision.

Completion requires:

- an explicit transaction/revision model for hierarchical changes;
- validation before commit;
- atomic publication to readers;
- deterministic normalization and deduplication;
- rejection of changes that would violate nesting;
- durable journal evidence sufficient for exact replay;
- concurrency, restart, and failure-injection tests.

The approved design is recorded in **Atomic Hierarchical Membership Contract and Implementation Plan** (2026-09-16). Implemented work includes the pure hierarchy contract, schema-v2 atomic persistence, bundled replay, atomic projection, v2 audit checks, separated poller registration, slot-time membership resolution, fail-closed missing-membership behavior, durable empty-channel skips, and a controlled JSON publisher. The September 21 session exercised published `r0`/`r1` with Uni=554 and Focus=40 through a full day: audit and replay passed with 2,342 events, 2,340 acquisitions, and 494,520 observations. Current-day `OV_DECISION` is now calculated from Schwab five-minute extended-hours candles for the complete opening Uni; a separately verified consumer ranks that immutable API evidence and produces Focus `r1`. ToS is not an input and publication remains an explicit operator acceptance step.

The September 23 API-only session repeated the same 554/40 hierarchy with
current Schwab OV evidence for all 554 opening-Uni symbols and zero acquisition
failures. Both pollers completed every scheduled slot; the journal passed its
full audit and deterministic replay. This closes the API-only opening gate but
does not replace the still-pending live concurrent hierarchy-transition test.

### CP-3 — Deterministic daily universe selector

**Status: Implemented and production-style validation completed; a newly
identified timing/volume-source validation defect requires correction before
the complete daily opening pipeline is declared operational.**

Build the next trading day's stable Uni set after the current regular session has completed.

Established design decisions:

- Use the just-completed regular-session **close**, not current intraday or extended-hours Last.
- Use the completed day's total volume for the initial `Volume >= 10,000` filter.
- If market capitalization is calculated locally, use the same completed-session close with the selected shares-outstanding source.
- Build after the regular-session close so the next trading day's universe is ready before premarket operation.
- Keep version 1 CLI/config driven and operational.
- Preserve deterministic inputs, normalized symbols, explainable rejection reasons, reproducible output, timestamped run records, and explicit dry-run/submit behavior where submission applies.
- The optional GUI is deferred and must not delay the selector.

The implemented selector now freezes the Nasdaq symbol directory, acquires a
batched Schwab post-close snapshot, calculates market capitalization from the
regular-session close and shares outstanding, and writes an immutable decision
ledger, pollable Watchlist, symbol list, and hashed manifests. A one-command
production workflow and operating runbook preserve the independently runnable
stages. The workflow also creates a strict schema-v2 opening `r0` proposal and
can publish it atomically to an explicitly selected journal root.

The September 18 production-style validation selected 554 symbols for the
September 21 opening roster. The motivating DAIC case was correctly included
from a $3.55 regular close, 4,105,261 shares of volume, 1,210,383 shares
outstanding, and a calculated market capitalization of $4,296,859.65. Opening
`r0` populates Uni from this roster while leaving Focus and Hot empty. The
September 21 opening `r0` and `r1` were published to a dedicated schema-v2
journal. Concurrent Uni and Focus polling completed the full session and the
journal passed audit and deterministic replay.

On September 23, running the workflow premarket for source session September 22
incorrectly returned `PASS` with only 133 symbols. The manifest showed 6,052
`volume_below_min` decisions versus 1,233 in the validated September 18
snapshot, demonstrating that `quote.totalVolume` was no longer reliable as
completed-session evidence after the next session had begun. That 133-symbol
proposal was preserved as failure evidence and never published. The live
session used an explicit, hash-identical 554-symbol carry-forward from the
frozen September 18 evidence; all downstream API-only stages then passed.

After the September 23 close, the workflow ran in its intended window and
selected 539 symbols for September 24, with 1,295 `volume_below_min` decisions.
Its opening `r0`, content SHA-256
`3c311dbf4543f23329cf8a56075e5c47b61aaf82d490b73908f05cf25f38a36e`,
was published to a dedicated September 24 schema-v2 journal and passed audit
and replay. The same-date post-close gate now rejects temporally invalid
snapshots and records its timing policy and volume semantics. It was
live-validated at 16:44 ET on September 24 and selected 545 symbols for the
September 25 opening.

### CP-4 — Observation Overlay/OOOHLCV diagnostic MVP

**Status: In progress. The domain contract, replay-causal preparation,
optional one-symbol visual surface, and immutable Schwab cache acquisition are
implemented; completed real-day validation remains.**

Build a read-only, replay-causal view for one symbol and one session using a
completed schema-v2 journal plus cached Schwab five-minute OHLCV. The MVP
overlays exact acquisition-time observations and statuses with membership
bands based on the highest active tier: gray outside Uni, cyan Uni, gold Focus,
and magenta Hot.

MVP guardrails:

- playback only; no live input or influence on pollers;
- one symbol and one session;
- completed journals opened read-only;
- cached OHLCV stored separately from the immutable observation journal;
- acquisitions become visible at `completed_at_utc` and membership at
  `effective_at_utc`;
- a completed five-minute candle becomes visible only after its interval
  closes, preventing look-ahead;
- no indicators, signals, news/events, holdings, multi-symbol or multi-session
  view, alternate provider, configurable period, or algorithm/backtesting use.

Acceptance requires deterministic timestamp alignment, Eastern Time
presentation, cache/source provenance, synthetic Uni→Focus→Hot/removal
coverage, and successful playback against the September 21 and—if the live
gate passes—September 23 schema-v2 journals. The overlay is supplemental
diagnostic evidence; journal replay and audit remain authoritative.

## 4. Near-term work

Near-term work should be handled in small, testable weekly increments—normally one or two implementable steps per weekly plan.

1. **Live-validate the 09:00 OV production cutoff.** Keep Focus at 40, run the existing acceptance gates, and preserve the immutable bundle and hierarchy evidence. Do not delay the opening or alter membership manually to chase retrospective `OV_FINAL`.
2. **Finish validating the guarded Observation Overlay/OOOHLCV MVP.** Exercise the immutable one-request cache acquisition and one-symbol causal playback against completed real-day journals without modifying live pollers or journals.
3. **Validate post-opening dynamic membership with live-like concurrent Uni and Focus polling.** Apply a new Focus change—preferably from a genuine LUDP/M event—while pollers are active. Prove atomic reader handoff, exact revision binding, replay, and audit; use the overlay as a diagnostic view rather than acceptance authority.
4. **Use September 11, September 14, September 15, September 21, September 23, and September 24 as standing real-day regression evidence.** Preserve the recorded per-day accounting and sequence evidence where the underlying journal is unchanged.
5. **Document `sync_csv_v2` operationally.** Expand its minimal README to cover the production launcher/stop workflow, transport semantics, testing, and separation from the future after-market archive utility.

## 5. Medium- and long-term work

### 5.1 Medium-term market-data and signal work

- Add HotWatchlist as a higher-frequency subset after atomic hierarchy changes are proven. Decide cadence, capacity, admission, eviction, and back-pressure policy from measurement.
- Build the Intraday Signals layer using current Uni/Focus/Hot observations plus historical features.
- Extend current-day API OV into historical 3/5/10/30-session analytics.
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
- Evaluate El-Cheapo runtime consolidation—preferably a supervisor that preserves separate worker fault domains—versus directly merging `scan_command_loop.py` and `scan_main_v2p0dev0.py` only after the coordinator concept and current scanner behavior are stable. Until then, keep them separate and establish a durable logging/audit contract across both components.

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
| Uni selector | Canonical symbol-directory source and availability; price/market-cap bounds; shares-outstanding source; handling of missing/stale fields; post-close acquisition window; completed-session volume source and rollover detection | Determines daily membership and reproducibility; September 23 proved that regular-trade date alone cannot validate `quote.totalVolume`. |
| Schwab authorization UX | Positive encrypted-config acceptance feedback; explicit forced browser reauthorization with recoverable token-database rotation | Prevents ambiguous silent waits and removes the current manual token-database rename procedure. |
| Trading calendar | Holiday, early-close, and next-trading-day logic | Required to interpret “completed regular session” correctly. |
| Hot | Poll frequency, size, admission/eviction, promotion/demotion, API budget | Must be evidence-driven and cannot break Focus/Uni schedules. |
| Focus publication | Relationship between API Focus, canonical coordinator membership, and ToS display membership | Prevents ToS from becoming an accidental source of truth again. |
| Journal retention | Duration for full SQLite journals; distillation format; archive and deletion policy | Current full-day volume is substantial; auditability must survive compaction. |
| Data quality | Treatment of invalid symbols, partial batches, stale timestamps, and API errors | A quote response is not automatically a valid observation. |
| Observation Overlay MVP | Completed-candle visibility, timestamp alignment, OHLCV cache/provenance, and causal joining of observations with hierarchy state | Prevents look-ahead and keeps the diagnostic view reproducible without altering live acquisition or the journal. |
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
- **El-Cheapo runtime consolidation and durable logging:** deferred until behavior, coordinator ownership, and recovery semantics are stable. Evaluate a supervisor versus direct merger of `scan_command_loop.py` and `scan_main_v2p0dev0.py`; first address the command loop's console-only logging, session-based log naming, rotation, startup identity, and durable command/result/postcondition audit records.
- **Full production trading/back-testing platform:** deferred behind data quality, replay fidelity, historical coverage, and risk controls.
- **Separate strategy/back-testing repository:** previously postponed; keep market acquisition/history and its foundational replay work together until a real ownership boundary emerges.
- **Complex source-priority and Watchlist-size policy:** deferred beyond the coordinator POC's intentionally simple rules.
- **Observation Overlay live mode:** deferred. The MVP is historical playback only and must not become an input to or burden on live pollers.
- **Observation Overlay multi-symbol, multi-session, and multi-day views:** deferred. This does not revive the separately tabled general dashboard multi-symbol filtering feature.
- **Observation Overlay indicators, signals, news/event markers, and holdings:** deferred until the diagnostic MVP and active-polling hierarchy transition are validated.
- **Observation Overlay configurable bar periods and alternate providers, including Massive:** deferred; the MVP uses cached Schwab five-minute OHLCV.
- **Observation Overlay algorithm, optimization, and backtesting integration:** deferred behind replay fidelity, historical-data quality, and the separate backtesting architecture.

### Superseded or rejected directions

- **Using the ToS Watchlist as the primary market-data universe:** superseded. ToS remains a display/execution adapter; Uni acquisition is API-based and independent.
- **Allowing producers to mutate ToS directly:** rejected by the coordinator architecture. Producers submit intent; the coordinator owns the target and the adapter performs the GUI work.
- **Treating accepted commands as proof of desired state:** rejected. Satisfaction requires observation/reconciliation.
- **Using intraday or extended-hours Last for the next day's deterministic price filter:** rejected. Use the completed regular-session close.
- **Building the selector GUI before the selector works operationally:** rejected as sequencing; the GUI remains optional and deferred.

A previously deferred capability may move forward only when its prerequisite has changed. For example, persistent journaling and replay were once deliberately postponed during early OV probing; they became active work after independent Uni/Focus acquisition was proven and durable evidence was needed. That changed evidence—not a silent reversal—justified the move.

## 8. Operational procedures and validation evidence

### 8.1 Daily operating model

1. After the regular session closes—and before the next session begins—construct the next trading day's stable Uni snapshot from frozen symbol-directory and completed-session evidence. Do not recreate a missed `quote.totalVolume` snapshot the following morning; use an explicit, documented fallback or stop.
2. Before the next session, validate configuration, credentials, output paths, symbol counts, revision identity, and scanner readiness where ToS publication is expected.
3. Run Uni and Focus as separate long-running processes. Current established slots are Uni at `:00`/`:30` and Focus at `:05`/`:20`/`:35`/`:50`.
4. Write acquisitions, observations, and membership revisions into the dated SQLite journal.
5. Treat invalid symbols, missing quotes, API errors, overruns, and stale timestamps as explicit evidence categories.
6. Use the dashboard against the selected daily journal. Both supported server-stop methods and single-day seek-to-historical-time are validated; journal/date selection remains external.
7. Publish to ToS only as an outbound display adapter. Do not export or read
   membership back; command completion is reported as submitted/unverified.
8. Preserve logs, run metadata, journal, raw `nasdaqlisted.txt` and
   `otherlisted.txt` snapshots, symbol-directory manifests, and relevant CSV
   evidence before any after-market distillation or archival step. These are
   currently local run artifacts; a formal retention/backup policy remains
   pending.

### 8.2 Established scanner command pattern

- MasterBot publishes scanner commands to the El-Cheapo `SCANCTRL` share.
- The command lifecycle separates incoming, accepted, processed, and rejected states.
- Use `mb-scan-status` as one health input, not as sole proof that all required processes and GUI conditions are healthy.
- Run the El-Cheapo command loop in display-only mode; scheduled and explicit
  exports remain suspended and export/add/resume-export commands are rejected.
- Before outbound replacement, make sure the ToS pane is a static Watchlist
  and ensure no manager window is covering the import dialog.
- Stop display-only operation with `mb-scan-command stop --wait 10`. A processed
  stop result with `shutdown=True` terminates `scan_command_loop.py`; use
  `Ctrl+C` only when the command channel is unavailable or the process fails to
  exit.

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
| 2026-09-16 | Atomic membership contract and persistence | Commits `dd84675` and `0a8f664`; pure normalized hierarchy contract and opt-in schema-v2 atomic persistence implemented with schema-v1 compatibility. |
| 2026-09-16 | Atomic hierarchy replay | Commit `4667768`; v1/v2 schema detection, one bundled v2 membership event, atomic projector/dashboard behavior, and replay CLI support validated with 181 tests. |
| 2026-09-16 | Dynamic poller handoff | Schema-v2 run registration separated from publication; slot-time membership provider, fail-closed lookup, durable empty-channel skips, controlled JSON publisher, and v2 audit checks implemented synthetically. Corrective review added latest-effective binding enforcement, journaled skip completeness, per-channel hash/provenance validation, publication-order enforcement, and concurrent atomic-read coverage. Full suite reached 211 tests before live-like process validation. |
| 2026-09-16 | Interrupted credential-expiry session | Schwabdev 3.0.5 entered interactive refresh inside an exclusive token-database transaction; an empty callback left that transaction open, blocked the second poller, and required process termination plus `mb-schwab-auth`. The schema-v1 journal remained replayable: 2 revisions, 2,304 acquisitions, 584,526 observations, 18 missing slots per channel, and sequence SHA-256 `91da0974e80051c00cd0a52686cfb4c4eb85516142072f362800a2745a80a398`. |
| 2026-09-21 | First full schema-v2 session | r0 Uni=554, r1 Focus=40; audit and replay passed with 2 hierarchy events, 2,340 acquisitions, and 494,520 observations. |
| 2026-09-22/23 | API-only OV transition | `mb_market_data` commit `48526d3` added complete opening-Uni Schwab candle acquisition; `schwab_watchlists` commit `96af1dd` made Focus r1 consume only verified API OV evidence. ToS display-only support was already deployed in `ToS_scanner` and `mb_tools`. |
| 2026-09-23 | First live API-only opening | Current OV evidence succeeded for all 554 opening-Uni symbols with zero failures; Focus r1 contained 40 symbols. Both pollers completed all 2,340 acquisitions and 494,520 observations with no skipped/missing slots or row mismatches. Audit and replay passed; sequence SHA-256 `9d8cddf55df11007b6ec6f771b2250d576e6f600d126011046c5525d99c3856a`. ToS received an optional 40-symbol unverified display snapshot. |
| 2026-09-23 | Daily-universe timing defect and recovery | A next-morning run falsely passed with 133 symbols and 6,052 volume rejections; it was not published. A hash-identical 554-symbol September 18 carry-forward supported the live downstream test. A proper post-close run then selected 539 symbols for September 24; its published r0 passed audit/replay. |

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
| D-003 | Preserve **Hot ⊆ Focus ⊆ Uni** through complete, atomically published hierarchy revisions. Invalid nesting is rejected before commit. | Active; hierarchy/session validation completed on 2026-09-21, with post-opening dynamic transitions still pending |
| D-004 | Use completed regular-session close and completed-day volume to build the next trading day's universe. | Active |
| D-005 | Keep universe-selector v1 CLI/config driven; GUI is deferred. | Active |
| D-006 | Producers express intent; the coordinator owns canonical state and publication. | Active |
| D-007 | For authoritative adapters, accepted is not satisfied and unknown outcomes must be preserved. ToS display-only publication is the explicit exception: it is reported submitted/unverified and does not gate canonical state. | Active, refined by D-026 |
| D-008 | Daily journals are the audit/replay basis; longer-term distillation must preserve reproducibility. | Active |
| D-009 | Keep `scan_command_loop.py` and `scan_main_v2p0dev0.py` separate during current work; revisit consolidation only after stable behavior, ownership, recovery, and durable logging are established. | Active |
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
| D-024 | Programmatic credentials use provider-specific encrypted `.ecfg` files owned by `mb_tools`, kept outside repositories, resolved by explicit path, service override, then `MB_VAULT` default, and restricted by machine/service ownership. Plaintext credential fallback is prohibited. | Active |
| D-025 | Current-day `OV_DECISION` is calculated from Schwab API candles over `00:00 <= start < 09:00 ET`; ToS volume is neither an input nor a required match. The production cutoff moved from 08:25 to 09:00 after the September 24 cutoff study showed 38/40 final-member agreement at 09:00 versus 33/40 at 08:25, while retaining 30 minutes of operational margin. | Active |
| D-026 | ToS roster publication is outbound and unverified. No ToS CSV export/readback is required for OV, Focus, or adapter confirmation. | Active |
| D-027 | After closing production defects exposed by the first live API-only session, the next feature priority is a playback-only, read-only, one-symbol/one-session Observation Overlay/OOOHLCV MVP. The concurrent hierarchy transition follows immediately afterward. | Active; September 24 production hardening is complete and the MVP resumes |
| D-028 | Observation Overlay expansion features—live mode; multi-symbol/session/day views; indicators, signals, events, and holdings; configurable periods; alternate providers; and algorithm/backtesting use—are deferred, not dropped. | Active |
| D-029 | A daily-universe snapshot must carry independently validated completed-session volume evidence. A next-morning `quote.totalVolume` snapshot is not accepted merely because the regular-trade timestamp still names the prior session. | Completed; same-date post-close gate live-validated September 24 |
| D-030 | Keep opening Focus at 40 symbols while operational experience and multi-session evidence accumulate. Membership-cutoff and size refinements must not block the next project milestone; their meaningful later test is coverage of symbols promoted—or that should have been promoted—into Hot. | Active |

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
- Implemented the pure hierarchy contract, schema-v2 atomic persistence, bundled replay, and atomic projection while preserving schema-v1 behavior.
- Added schema-v2 audit labeling and checks for bundle completeness, nesting/content validity, sequential revisions, effective-time order, and exact acquisition binding.
- Separated poller run registration from hierarchy publication. Pollers now resolve membership at scheduled slot time, fail closed if no revision is effective, and durably report empty-channel skips without issuing an empty Schwab request.
- Added a controlled JSON hierarchy publisher for r0/r1 testing; coordinator-side publication integration and concurrent live-like transition evidence remain pending.
- Corrective review closed stale acquisition binding, sidecar-only empty-slot evidence, unchecked per-channel provenance/hash, and publication-time regression gaps; added concurrent readers-versus-publication coverage.
- Diagnosed the interrupted-session token lock as a Schwabdev 3.0.5 failed-authorization transaction leak rather than a quote-journal lock. Prepared a Schwabdev 4.x requirement, read-only token-status command, and fail-before-artifacts poller horizon gate; Windows installation and live smoke validation remain pending.

### 2026-09-19 — Credential architecture contract

- Accepted `mb_tools` as the canonical owner of the cross-project Credential Storage and Resolution Contract.
- Standardized provider-specific encrypted `.ecfg` files outside source repositories, explicit resolution precedence, least-privilege machine ownership, redacted failure behavior, and lifecycle requirements.
- Recorded Schwab as implemented and Pushover/Massive as planned rather than operational.
- Kept operator procedures and User Notes separate until each provider integration has tested commands and failure behavior.

### 2026-09-22 — El-Cheapo runtime and logging follow-up

- Confirmed that `scan_main_v2p0dev0.py` writes durable session logs whose filenames are based on process start time rather than calendar date.
- Confirmed that `scan_command_loop.py` currently logs to its console rather than a durable file, leaving command-plane actions difficult to reconstruct after console output is lost.
- Deferred El-Cheapo runtime consolidation and durable logging; keep the two processes separate until the behavior, ownership, recovery, and logging contract are stable.

### 2026-09-22 — Observation Overlay reprioritization

- Promoted the guarded Observation Overlay/OOOHLCV diagnostic MVP to the first development milestone after the live API-only opening and audit pass.
- Kept the MVP playback-only, read-only, one symbol, and one session, with cached Schwab five-minute OHLCV and replay-causal visibility.
- Moved the genuine concurrent schema-v2 hierarchy transition—preferably from a new LUDP/M event—to immediately after the overlay MVP.
- Recorded all excluded overlay expansion features as deferred rather than dropped.

### 2026-09-23 — API-only live validation and daily-universe hardening

- Recorded the first full schema-v2 session: Uni r0 contained 554 symbols,
  Focus r1 contained 40, and the journal contained 2,342 events, 2,340
  acquisitions, and 494,520 observations.
- Implemented current-day API OV acquisition from Schwab five-minute candles
  over `00:00 <= start < 08:25 ET`, with complete-Uni coverage, immutable
  evidence hashes, and fail-closed production eligibility.
- Implemented independent API-OV evidence verification and deterministic
  Focus-r1 production in `schwab_watchlists`.
- Made ToS an outbound display-only adapter: no ToS CSV export, membership
  readback, or ToS volume comparison is part of OV, Focus, or canonical-state
  acceptance.
- Completed the first live API-only opening with 554 Uni and 40 Focus symbols.
  Both pollers completed all 2,340 acquisitions and 494,520 observations; audit
  and replay passed with no skipped/missing slots or row mismatches and sequence
  SHA-256 `9d8cddf55df11007b6ec6f771b2250d576e6f600d126011046c5525d99c3856a`.
- Recorded that the session used an explicit 554-symbol September 18
  carry-forward because a September 23 premarket attempt to reconstruct the
  September 22 universe falsely passed with only 133 symbols. The invalid
  proposal was not published.
- Ran the universe workflow after the September 23 close and selected 539
  symbols for September 24. Published r0 hash
  `3c311dbf4543f23329cf8a56075e5c47b61aaf82d490b73908f05cf25f38a36e`
  passed audit and replay.
- Promoted timing/volume-source validation ahead of the Overlay MVP and added
  encrypted-config acceptance feedback plus forced Schwab reauthorization to
  the immediate production-hardening backlog.
- Confirmed that a processed display-only stop command sets `shutdown=True`
  and terminates `scan_command_loop.py`; manual `Ctrl+C` is fallback-only.

### 2026-09-24 — Repeated production validation and OV cutoff decision

- Completed a second full schema-v2 session with Uni r0=539 and Focus r1=40:
  2,340 acquisitions and 482,820 observations, with no skips, missing slots,
  request errors, invalid symbols, or row mismatches. Audit and replay passed
  with sequence SHA-256
  `5650e9a355e9979a309d4fdcf3eb25da9a45f5fb275fb84519c07d122cf6358e`.
- Live-validated the same-ET-date post-close daily-universe gate at 16:44 ET
  and produced the September 25 opening Uni of 545 symbols.
- Added explicit encrypted-configuration acceptance feedback and recoverable
  `mb-schwab-auth --force-reauthorize` support in `mb_tools` commit `abdceb1`.
- Added a separate retrospective cutoff study and reproduced all 539 immutable
  08:25 production values exactly. For top 40, agreement with `OV_FINAL` was
  33 at 08:25, 38 at 09:00, 39 at 09:15, and 40 at 09:25.
- Adopted 09:00 ET as the production `OV_DECISION` cutoff. It materially
  improves final-membership agreement while retaining 30 minutes for the
  current sequential acquisition and operator-controlled opening procedure.
  Keep 09:15 and 09:25 as later candidates as practice and automation reduce
  launch latency.
- Retained Focus size 40 pending multi-session evidence and Hot-promotion
  criteria. Fine tuning of cutoff and size does not block the Observation
  Overlay MVP or the subsequent concurrent hierarchy work.
- Began the Observation Overlay MVP without waiting for the next-session 09:00
  cutoff validation. Fixed separate inclusive visibility boundaries for
  membership, quote acquisitions, and completed five-minute candles; added an
  immutable Schwab OHLCV cache contract and pure one-symbol/session preparation
  layer. Live polling and journal persistence remain unchanged.
- Added the optional one-symbol dashboard surface. It advances, restarts, and
  seeks with the main replay clock; renders completed five-minute candles,
  volume, numeric quote outcomes, and hierarchy bands; and is absent unless an
  immutable cache is explicitly supplied. Cache acquisition and completed-day
  validation remain the next increment.
