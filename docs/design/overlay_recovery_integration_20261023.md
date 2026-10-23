# Overlay integration with Nasdaq and authenticated Hot recovery

October 23, 2026 — **architecture review and read-interface requirements only.** No Overlay runtime change or new validation result is claimed. Source inspected at market-data checkpoint `4d32131519e12ff4a56b6194c4de8c4d7e794a62`, local equivalent tree `825cbb97d693f5f4c6fc58c4a1b463cb5027a1d4`.

The backend definition is [integrated_hot_recovery_experiment_20261023.md](https://github.com/DanArizona/mb_watchlist_coordinator/blob/experiment/integrated-hot-recovery-2026-10-23/docs/design/integrated_hot_recovery_experiment_20261023.md). Review the interface while defining that controller; implement and qualify the Overlay adapter after backend qualification.

## Current source findings

| Source | Existing behavior relevant to this work |
| --- | --- |
| `src/mb_market_data/observation_overlay.py`, `ObservationOverlayProjector.apply/snapshot` | Already accepts increasing schema-v2 membership revisions, derives the selected symbol's highest tier, retains quote outcomes and clips membership/quote evidence to replay availability. Dynamic membership is an existing capability. |
| `src/mb_market_data/overlay_halts.py`, `OverlayHaltInterval` and `load_overlay_halt_intervals` | Loads a static HISTORICAL record list into halt start, optional trade-resumption endpoint and reason. The interval value has no event/update identity or source receipt/knowledge timestamp. |
| `probes/dashboard_quote_observation_journal.py` | Binds one explicit journal and one immutable selected-symbol OHLCV cache at launch; optional normalized historical halt evidence is loaded once into the projector factory. |
| `src/mb_market_data/quote_dashboard_replay.py` | Drives the journal event factory and optional Overlay projector through a selected timeline. It is not evidence of a qualified new live Nasdaq/recovery tailing interface. |

The accepted historical Overlay remains useful retrospective context. A static final interval alone cannot establish when its resumption endpoint first became known. Add an explicit as-known event/update projection instead of presenting final historical facts as if they were known throughout a live/recovery replay.

## Required read contract

1. **Explicit bindings.** Identify the experiment/session, journal/schema, source/recovery ledgers and their validated prefix identities. Refuse mixed dates, changed identity or implicit path discovery. The first adapter reads one experimental journal with its corresponding source evidence. Combining production Uni/Focus and experimental Hot is a separate multi-journal identity/clock/deduplication decision.
2. **Separate membership, source facts and acquisition outcomes.** Membership comes from acknowledged Coordinator publications at their effective times. Captured Nasdaq facts remain visible even when they cause no new request, are unresolved or are rejected by normal admission. Quote/invalid/missing outcomes retain their actual statuses and channels. The Overlay owns none of those ledgers.
3. **Versioned source updates.** Preserve event ID (symbol plus original halt identity in source/session context), update identity/sequence, reason, reported halt time, quote/trade-resumption facts, capture/query provenance, original source receipt/knowledge time and recovery receipt time. Keep the earlier versions available so later corrections/resumption fields do not overwrite replay history.
4. **As-known visibility.** Reveal an update only when the bound backend evidence says it became available; show original market-event time separately. A recovered earlier halt may annotate an earlier market time while its discovery/knowledge marker remains at recovery time. Do not reveal a later-learned release endpoint during an earlier replay position. Preserve the legacy retrospective mode as a separate supported behavior.
5. **Commit and receipt uncertainty.** Preserve scheduled slot, provider response time and journal-commit-observed receipt separately. Recovery may commit an older retained response later. If an exact knowledge/commit observation is missing, mark it unknown rather than deriving it from a filesystem timestamp or fabricating a precise receipt.
6. **Health and continuity.** Expose backend observation time, feed freshness, recovery state, explicit query scope, remaining gaps, native coverage and unknown dispatches. Distinguish an empty Hot roster, an unreconciled scope, unavailable source evidence and a stopped collector. Absence of a new quote is not automatically evidence of collector failure.
7. **Acquisition-free view.** Reading, changing a selected symbol or reconnecting must not issue implicit feed/broker calls, start workers or publish membership. A newly admitted symbol may have no prepared candle cache; make that availability explicit. A deliberate live candle acquisition path needs its own integration/credential/budget qualification.

## Follow-on architecture work and acceptance

- Introduce a versioned read adapter over bound backend facts/status and the existing journal projection. A completed backend validation receipt is the publication boundary for displayed state; UI code does not become the Coordinator.
- Decide how an explicit live adapter tails and resumes from durable source/update cursors, rejects changed prefixes and rebuilds after reconnect. Keep the historical replay controller's accepted behavior intact while adding the selected live/as-known mode.
- Support a symbol that enters Hot after startup, including outside-opening-Uni admission, with the correct effective membership band. Define symbol/cache selection behavior explicitly rather than silently changing the chart or fetching candles.
- Test live arrival, duplicate delivery, source outage, recovered earlier halt, later resumption update, changed identity, UI reconnect, saved quote committed after restart and playback before/after each knowledge boundary. Compare displayed values to retained backend receipts and native replay; retain unknown fields honestly.
- Preserve existing historical Overlay rendering, controls and replay regressions when affected. This work does not reopen deferred blank-Seek diagnosis, candle supervision policy, trading strategy design or production cadence adoption.

No live Overlay adaptation is delivered by this note. The next backend implementation slice is authenticated fresh-controller startup before initial journal/`poll_run` creation; the Overlay contract above supplies its required evidence fields.
