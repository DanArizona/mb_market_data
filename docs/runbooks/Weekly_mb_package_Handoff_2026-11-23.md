# Week 2026-11-23 — mb_package verified handoff and GitHub checkpoint

**Scope:** The entire mb_package project; current focus is read-only Overlay
Release A and reliable ordinary production operation.
**Evidence cutoff:** Monday 2026-11-23, before the usual production opening.
**Status:** Written on the isolated `experiment/overlay-release-a-historical-20261122`
branch. This document is **not** an authorization to merge, install, restart,
or change production. No code changes accompany this checkpoint.

Use this as supplemental evidence for the recurring **Weekly mb_package Project
Review**, the canonical roadmap, and a new working chat **Week 2026-11-23**.
New weekly review results may supersede specific observations below;
retain the original date/time and exact audit verdict of each cited session.

## 1. Production baseline and verified evidence

Accepted normal architecture:
`completed regular-session daily Uni selection → next opening r0 →
hash-verified API-only OV/Focus r1 → automatic Nasdaq and approved manual
membership revisions → interleaved Uni/Focus/Hot polling → dated schema-v2
SQLite journal → audit and deterministic replay → independent read-only Overlay`.

- The **one-unlock-daily-flow-v1** controller is the accepted production
  operating model. Standalone legacy dual poller windows are retired.
  On Monday November 23, start the existing installed controller at the
  **usual time and by the established instructions**; do not use an
  experimental checkout or new viewer launcher to start polling.
- `Hot ⊆ Focus ⊆ Uni` is preserved through atomic effective membership
  revisions; the daily journal is authoritative. Thinkorswim is an optional
  unverified *outbound display* adapter, not market-data evidence.
- The deterministic daily universe selector and schema-v2 opening
  hierarchy are completed capabilities. Opening Focus uses verified
  API-only overnight-volume evidence for the exact opening Uni, not
  a ToS CSV or an `OV_DECISION` display field.
- Live automatic Nasdaq LUDP/M admission and ordinary manual
  promotion/demotion have already been demonstrated. Ordinary demotion
  remains accepted for halted symbols; **Suppress Automatic** is deferred.
  Exact provider `EQUITY` identity remains mandatory for outside-Uni
  operator admission; exchange/OTC/subtype/active-listing refinements
  are explicitly **deferred**, not newly required.
- Latest completed **full-day** evidence in the preceding review is
  November 13: Uni **780/780**, Focus **1,560/1,560**, Hot
  **9,354/9,360** (six missed slots); **strict audit FAIL**.
  There were 48 membership revisions, **11,694 acquisitions** and
  **737,137 observations**. Final r47 membership was 575 Uni / 64
  Focus / 25 Hot. Two deterministic replays had matching sequence hash
  `e819c6f1…`. Earlier full-day Hot misses: November 11 **9**, November
  12 **8**. Do not reclassify these as audit passes.
- The November 16 weekly review recorded a **09:32:05 ET opening
  checkpoint**, not a whole day: 5 Uni, 8 Focus, 50 Hot acquisitions,
  zero missed slots *at that time*, Focus r1 published,
  opening hierarchy 552 Uni / 44 Focus / 4 Hot, selection **DEGRADED**.
  Exact DEGRADED reason, source of fourth Hot member and complete-day
  result were not established by that review. No new full-day completion
  is asserted in this handoff.
- On November 13, one-day storage was approximately 383.6 MiB of
  SQLite journal plus 1.7 GiB of evidence. Data retention, historical
  distillation, and archive separation merit future planning, but must
  not distract from core polling health or current Overlay work.
- Authentication fix in `DanArizona/mb_tools` is on main through
  `47e07bdd1435172ef0337b4c90909d9e01d27a51`. Normal session
  authorization/expiry horizons and exact installed source identity
  remain per-day checks; do not extrapolate authorization from one
  earlier morning checkpoint.

## 2. Overlay: accepted implementations and Windows evidence

**Historical source**:
`experiment/overlay-workspace-layout-20261116` at
`1b3000489789c4540541c5a8eb3a7c920f49e135`.
The consolidated read-only **Release A** branch is
`experiment/overlay-release-a-historical-20261122`. The integration
commit `cfadea5013eb9b3d0ffdc3db8e74a13e1bf0eacd` took exactly
**19 changed/added historical-workspace blobs** from the accepted source
onto main `de1e5544f583c66ebd80355ad2cbaa55ef8860a6`, plus
documentation. Remote Git tree comparison on November 23 confirmed
all 19 accepted source blobs remain exact, no source deletions, and
unchanged production `daily_runtime.py`, `daily_flow.py`,
`daily_membership.py`, `daily_live_overlay.py`, and `overlay.cmd`.

**Windows offline:** On MasterBot, user ran
`call verify_overlay_workspace.cmd` in `dashing`; **72/72 PASS**
(Dash 2.18.2, Plotly 6.5.2), no provider calls or production changes.
The old integration source inventory intentionally does not match
the newer UI source; do not construe `source_inventory_verified=false`
as a regression.

**Real production evidence:** The local Dash dashboard opened
the completed November 13 schema-v2 journal with **11,742** events
projected and genuine Nasdaq `events.json`, selected ONFO,
quote-only (no five-minute candle cache). At approximately **09:33 ET**:
515 Uni-only / 40 Focus-only / 6 Hot; ONFO at r5, $0.72, Halted,
33 visible quote outcomes. At approximately **10:28 ET**:
512 Uni-only / 40 Focus-only / 18 Hot; ONFO r30, $1.00,
Normal, 1,697 quote outcomes. **Backward seek to 09:33:13**
restored the earlier counts, effective revision, quote points,
price and security status. Nasdaq markers obey replay visibility.
Historical is read-only; no manual editing controls are present.
The viewer's **Stop server** returned CMD to prompt and an
independent TCP listener check returned **`PORT_8051_FREE`**.
This qualifies startup, historical reconstruction and clean
shutdown/port release, not every production release gate.

**Remaining historical qualification:** Monitor at the early replay
position showed 103 projected events, 4,794 observations and
**"Acquisition health · not attached"**. The external strict journal
audit must remain **FAIL** for Nov 13's six Hot misses; actual
health/audit attachment is pending. A genuine validated same-symbol,
same-date five-minute Schwab OHLCV cache has not been visually
qualified in this Release A checkout. The Charts subtitle incorrectly
implies completed candles even when the accurate count is zero.
Repeated real-day viewer restart and independence during an active
collector session remain later checks.

**Accepted synthetic Membership UX** (SEPARATE Release B source):
`experiment/overlay-membership-bridge-20261119` at
`c2a9a1173e776205d2c8128a56ae1daf82a9eb8f`.
User verified **119/119 Windows tests** and interactive Firefox
outside→Uni AMD admission, Find synchronization after a prior
REALLY lookup, Uni→Focus drag/Preview/Confirm, effective revision,
correct per-tier counts and highlighted destination row.
This is a disposable synthetic demo; **no real Membership GUI
writer is approved for production**. Preserve its UI design for
the read-only Historical membership tab, without importing its
drag, Preview, Confirm or synthetic worker into Release A.

**Existing main live viewer:** `src/mb_market_data/daily_live_overlay.py`
and `overlay.cmd` already provide a **separate read-only quote-only**
local viewer of committed observations with no SDK login or journal
writes. This is NOT yet connected to the full tabbed workspace.
Do not portray Real Time mode in the full historical workspace
as qualified; it is still disabled.

## 3. Release A plan, priority and gates

Keep the accepted order: **1 → 2 → 4A → 3 → 4B → 5**.

- **1 — Historical code integration: DONE** (isolated experimental
  branch, never installed).
- **2 — Completed-session Historical qualification: substantial PASS
  evidence** (72 Windows tests, real November 13 historical views,
  causal backward seek and browser Stop/port release); pending health
  attachment, genuine OHLCV cache and remaining operational checks.
- **4A — NEXT: isolated standalone session attachment.** Develop a
  read-only launcher that identifies a specified active or completed
  daily session, validates root/plan/journal identity and schema-v2
  membership contract, and attaches the local viewer. Start with
  explicit paths and safely bounded discovery; reject mismatches,
  ambiguity, missing files, and wrong-session data. No authentication,
  new Schwab client, membership writer, polling changes, or controller
  startup. Qualify offline against completed retained data first,
  then attach during a normally running production session with
  attended read-only verification. Stopping the viewer must not
  affect polling or Nasdaq/membership worker processes.
- **3 — Full Real Time workspace adapter:** reuse the existing live
  read-only journal adapter in the full tabbed Overlay; display
  committed quotes, effective membership, Nasdaq observations and
  live health with correct staleness/reconnect/session rollover
  behavior. Do not fetch/fabricate OHLCV as part of a quote-only mode.
- **4B — Routine launcher/lifecycle integration:** only after
  the Real Time adapter is qualified; add convenient attachment
  and independent restart/stop to established daily procedures.
  Do not change installed production controller without approval.
- **5 — Integrated Windows/attended live QA and release:** include
  final source identity, performance, live independence, audit warnings,
  operator runbook, rollback and explicit merge/deployment approval.

**User-requested Release A interface refinements, recorded November 23:**
1. Replace the old Historical Membership page *presentation* with
   the accepted newer three compact tier lists + shared Find +
   inspector, strictly **read-only** with historical messaging.
2. Show genuine cached historical five-minute candles and volume;
   no cache means labeled quote-only.
3. Add a **chart symbol selector** to Graph Setups sidebar and
   synchronize with selecting a single Membership member. This is
   *single-symbol context selection*, not the previously tabled
   multi-symbol simultaneous dashboard filter.
4. Add a historical **date calendar**, highlighting only accessible,
   validated completed journal dates. Date changes must switch
   session, symbol/cache, Nasdaq evidence and replay clock atomically.
   Implement after/with 4A's session catalog. This newer explicit
   user request does **not** revive general multi-day backtesting.
5. Attach Monitor's real acquisition health/audit evidence, with
   correct distinction between missing attachment and a passed audit.
6. Fix quote-only subtitle so it does not imply OHLCV exists.

First three interface items are desired before final production
acceptance; exact sequencing with 4A/3 can proceed incrementally.
The calendar is planned after session discovery. No user-approved
Production Release A merge or deployment has been performed.

## 4. Next working week — no more than three priorities

1. **Preserve normal production operation and capture full-day
   validation.** Begin today's ordinary one-unlock daily polling
   with the installed production controller at its usual time.
   Watch authorization horizon, selection state, revision integrity
   and every Hot slot. At close, retain strict audit, two replay
   comparisons, final counts, service exits, next-day r0, and
   exact installed source identity. Do not declare Hot audit PASS
   without a zero-missing, verifiable completed journal.
2. **Implement and qualify Release A 4A in the separate experimental
   checkout.** Develop safe session detection/attachment without
   touching production. Start with completed-session tests.
   If authorized and the normal session is running, verify
   read-only attachment and independent viewer shutdown.
3. **Finish read-only Historical readiness incrementally.**
   Attach health/audit status, integrate the accepted compact
   read-only Membership layout, qualify genuine OHLCV,
   add chart-symbol selection and plan the date calendar
   using 4A's validated session catalog.

## 5. Decisions, risks and intentionally deferred work

- Historical mode MUST remain read-only; manual GUI publication is a
  separately qualified **Release B** gate. Existing worker remains
  sole publisher. Preserve Preview/Confirm, CSRF/localhost,
  same-session identity, worker receipt, idempotency and
  effective revision checks when Release B is addressed.
- No new stricter manual admission eligibility policy, and no
  `Suppress Automatic` feature: both remain **deferred**.
  Ordinary halted-symbol demotion remains accepted.
- Do not revive ToS CSV or GUI readback as production proof.
- Universe selector GUI, dashboard print polish, multi-symbol
  simultaneous dashboard filtering, indicators/signals,
  multi-session analytics/backtesting, live candles acquired by
  Overlay, and new trading automation remain deferred.
- Additional workstreams without newly verified completion:
  `mb_watchlist_coordinator` mainline/history consolidation;
  `schwab_watchlists` and ToS adapter upkeep;
  `sync_csv_v2` operating documentation (separate from archival);
  MasterBot/El-Cheapo transport/logging; historical archival,
  retention and signal/distillation architecture.

## 6. GitHub remote state verified Monday 2026-11-23

Remote branch heads at checkpoint time (the Release A branch will advance
when this handoff document is committed):

| Repository / branch | Verified SHA |
| --- | --- |
| `DanArizona/mb_market_data:main` | `de1e5544f583c66ebd80355ad2cbaa55ef8860a6` |
| `mb_market_data:experiment/overlay-release-a-historical-20261122` (pre-handoff) | `8e53e04d7d555586b259f7e9e8a834a3452d74a0` |
| `mb_market_data:experiment/overlay-workspace-layout-20261116` | `1b3000489789c4540541c5a8eb3a7c920f49e135` |
| `mb_market_data:experiment/overlay-membership-bridge-20261119` | `c2a9a1173e776205d2c8128a56ae1daf82a9eb8f` |
| `DanArizona/mb_tools:main` | `47e07bdd1435172ef0337b4c90909d9e01d27a51` |
| `DanArizona/mb_watchlist_coordinator:main` | `3e31fefd67bdac38ac805ed70dd49ad249e0f0dd` |
| `DanArizona/schwab_watchlists:main` | `e2ef6f62856ba3226c41456b7779951aa8640cbf` |
| `DanArizona/ToS_scanner:main` | `751a8e43235717d45d0d57c318dd65136b5068d1` |

Verified remote histories/trees do **not** prove that every local
Windows worktree is clean or that all local commits were pushed.
Do not conflate `mb_market_data:main` with the exact installed
production controller source until it is independently checked.

The historical Release A branch had **19 accepted source blob changes +
one existing runbook**, all source blobs identical to the accepted
historical source. There were **no deletions** relative to main.
The production controller, membership publisher and existing live
viewer were byte-for-byte unchanged. Only documentation changes
were made after historical integration.

The weekly automation **Weekly mb_package Project Review** (ID
`6a9ab24d05348191a6cf63e4a1621e7d`) is **enabled**,
America/Chicago **Mondays 8:00 AM** with flexible scheduling.
Last completed run: Nov 16; next scheduled run: Nov 23.
It produces a weekly review and explicitly must **not**
modify repositories/files/tasks. Prior reviews have appeared in
the chat **Universe Selector Next Steps**. Confirm today's actual
placement/output; the review may have newer evidence.

## 7. Suggested new-chat opening prompt

> **Week 2026-11-23 — mb_package.** Continue from the verified handoff
> `docs/runbooks/Weekly_mb_package_Handoff_2026-11-23.md` on
> `DanArizona/mb_market_data:experiment/overlay-release-a-historical-20261122`.
> Also review today's `Weekly mb_package Project Review` output in
> `Universe Selector Next Steps`, if available, and the canonical roadmap.
> Prioritize normal one-unlock daily Uni/Focus/Hot production health and
> strict audit/replay evidence; alongside it, develop **Release A Step 4A**
> (independent, read-only attachment to the current daily session) in
> the isolated checkout. Release A Steps 1 and most of 2 are qualified:
> 72/72 Windows tests, genuine November 13 Historical playback and
> backward seek, clean viewer shutdown and port 8051 free. The audit
> remains FAIL for six missed Hot slots, and genuine OHLCV / health
> attachment and accepted Historical UI refinements remain pending.
> Synthetic Membership GUI is qualified at 119/119 but is **Release B
> only** for write controls. Preserve all existing deferrals and
> keep production polling, `main`, credentials and installed paths
> untouched absent explicit authorization. Use Windows 11 CMD,
> `dashing` for Overlay and `sea-green` for ordinary production,
> with password-prompting commands individually separated.
> Explain any conflict with the weekly task output rather than
> silently overwriting accepted facts.
