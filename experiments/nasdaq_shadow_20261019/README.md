# Bounded Nasdaq shadow review

This isolated increment connects fresh CURRENT Nasdaq capture to the accepted
source-owned producer, serialized Coordinator publication and guarded native Hot
collector. It never installs code into production or enables production Hot.
Production Uni/Focus and the independent production-health monitor keep running.

## Commands

Extract the unique ZIP beneath the production repository's `output` directory.
Use the existing Windows CMD / Conda `sea-green` environment. Every command runs
bundled source with `python -I -B`; no pip install or production checkout change.

* `19_verify_nasdaq_shadow.cmd` is OFFLINE ONLY. It denies network access,
  runs 60 affected integration checks, then starts independent Coordinator and Hot
  processes. A synthetic Nasdaq producer in the parent drives an outside-Uni
  arrival after one synthetic HTML interruption, repeated/resumption updates and
  a second-symbol capacity rejection. Expect PASS_WITH_WARNINGS
  (SYNTHETIC_TRANSITION), feed RECOVERED_WITH_GAPS, exactly 3/3 Hot acquisitions
  and three empty slots. The warning is the deliberately injected feed gap;
  the offline verification receipt qualifies only this expected recovery case.
  Scheduling uses actual elapsed time with an explicitly labeled future UTC
  offset; native journal publication timestamps retain actual UTC. No provider,
  password or private host evidence is accessed. About 35 seconds plus tests.
  The extended checks cover a complete logical 450-slot tail, hard call and
  exclusive endpoint guards, late discovery/publication, discovery closure and
  separate feed resumption reporting. The logical tail is not 15 minutes of
  elapsed provider-capacity evidence.
* `20_run_nasdaq_shadow.cmd OPENING_R0 FOCUS_R1 --market-open-confirmed` is the
  separate, explicit LIVE invocation. It requires that this exact package passed
  command 19, same-day opening r0/Focus r1 proposals, an existing encrypted Schwab
  configuration, normal credential preflight and the operator's regular-session
  confirmation. Launch 09:30 <= ET < 15:40 on a normal weekday session. No holiday
  calendar is inferred. This command asks for the encrypted-config password once.
  Put it alone or last in its CMD block; do not paste subsequent commands into
  its password prompt. The worker receives credentials over its owned stdin pipe,
  never a file, command-line argument, environment variable or persisted log.
* `22_run_extended_nasdaq_shadow.cmd OPENING_R0 FOCUS_R1 --market-open-confirmed`
  selects the separate longer LIVE preset after this exact package passes 19.
  Launch 09:30 <= ET < 15:25 on a confirmed normal weekday session. Discovery
  lasts at most 15 minutes; no selected symbol ends the run then. An approved
  selection gets at most 30 additional seconds for publication/first-slot
  eligibility, followed by up to 15 minutes of Hot polling from that first slot.
  The acquisition envelope is at most 30m30s from scheduled startup, plus
  bounded startup/cleanup. Credential preflight covers 35 minutes plus the
  existing refresh margin. A late lookup cannot admit a symbol after discovery
  closes. Failure/stop rules can end any phase early.
  The hard quote limits are 3 resolution + 450 Hot = 453 calls; RSS stays at
  least 60 seconds after completion, at most 31 attempts across the whole run.
  Production Hot remains disabled. The request still expires at session close;
  the 15-minute collector cutoff does not cancel it.
* `21_stop_nasdaq_shadow.cmd` writes a stop request to this package's currently
  owned shadow run. Run it in another `sea-green` CMD window if needed. Ctrl+C
  in the run window is also supported. Only explicit child-process handles are
  terminated; no production PID discovery, console matching or broad taskkill.

A live run is not launched by verification. Run one review experiment at a time,
and keep the production-health window visible. A successful command returns to
its CMD prompt. Save the console summary and exact output directory for review.

## Explicit review choices

* CURRENT feed only; LUDP/M; regular-session halt and receipt windows. The first
  successful snapshot is baseline only. Its events and later discoveries whose
  halt times precede that knowledge boundary cannot become new requests.
* Per-run immutable identity; stable local source ID `nasdaq-shadow` across
  reopen. Event ID remains symbol + canonical halt timestamp. Mutable reason or
  resumption updates and repeated feed rows do not duplicate requests. Later
  distinct halts for the same admitted symbol get separate owned requests.
* Up to 15 minutes of live observation, RSS polling no faster than 60 seconds
  from the preceding attempt's completion (valid or unavailable), 15 attempts
  maximum, 3-second timeout and bounded response size. Nasdaq's
  [Trade Halt RSS guidance](https://www.nasdaqtrader.com/Trader.aspx?id=TradeHaltRSS)
  says it updates once per minute and clients should query at most once per
  minute. The earlier two-second RSS configuration is superseded; preserve its
  failed runs. The offline synthetic fixture keeps its two-second feed interval
  without network access. Three consecutive feed transport failures stop this
  review. Transport and unusable-body failures share that same three-failure
  limit. HTTP 401/403/429, redirects, oversize bodies and error responses carrying
  Retry-After stop immediately. No challenge execution, cookie replay, identity
  spoofing, redirect following or extra retry loop is introduced. Invalid or
  conflicting semantic feed evidence still fails closed. No catch-up requests.
  These 15-attempt/15-minute bounds describe the short preset. The explicit
  command 22 bounds above extend the acquisition window and feed-attempt cap.
* Fresh `nasdaqlisted` and `otherlisted` captures supply the immutable session
  identity catalog. Test issues, ETFs, ambiguous aliases and punctuation variants
  are excluded from this small review; no price/volume/market-cap daily screen
  applies to an exceptional addition. Raw directory bytes/hashes and actual
  acquisition receipts are retained privately in the run.
* At most three broker identity lookups, each a one-symbol quote request. Require
  the exact returned symbol, `assetMainType=EQUITY`, `assetSubType=COE`; this
  confirms only the response identity. An invalid/missing/unmatched response
  leaves identity unconfirmed and creates no membership request. It establishes
  neither cause nor tradability. A dispatched lookup with no durable decision
  stays explicitly unknown and is never automatically reissued.
* Admit at most one distinct Hot symbol and one outside-opening-Uni addition.
  Multiple arrivals are considered in halt-time/event-ID order. Every rejection
  has a durable gate disposition; rejected source commands remain observed but
  undelivered, not silently acknowledged as Coordinator requests. Selected
  delivery retains the accepted producer's exact request-commit/ACK contract.
* Parent memberships publish together, retaining Hot subset Focus subset Uni.
  Opening membership stays separate; additions stay in session Uni. Requests
  expire at regular close; resumption/feed disappearance does not withdraw them.
  A bounded run ends acquisition without inventing a request cancellation.
* Only experimental Hot acquires quotes, every 2 seconds for up to 60 seconds
  from the first eligible slot, maximum 30 calls. Broker resolution + Hot has a
  durable shared hard cap of 33 calls. Empty Hot slots issue no provider request.
  Ordinary production Uni/Focus are not duplicated in this increment.
  Command 22 instead permits a 900-second tail and 450 Hot/453 total calls.
* The Hot tail end is exclusive. Published membership defines that cutoff even
  without the parent's optional `hot_window.json` note. A slot at/after it is
  refused before native acquisition dispatch, independently of a wall-clock stop
  check. The quote adapter checks again before reserving a call and before
  provider dispatch. Absolute target-time rechecks handle early relative wakes.
* Existing .75s dispatch gate, integrity checks, unknown-outcome gaps and
  no-catch-up behavior remain. Publication polling/lead is .25/.5s; policy
  grace/residence is 60/120s with the existing explicit cap selector. These are
  review settings; this run does not introduce manual restrictions or policy
  tests. No setting is adopted for routine production.

## Evidence and acceptance

Every attempted feed capture saves raw.xml (possibly empty/partial, explicitly
labeled), allowlisted HTTP metadata in http.json, and a capture.json outcome.
An fsynced receipt ties the metadata and raw hash to the attempt. The final
analysis verifies unavailable captures as well as successful ones. HTTP status,
final/requested URL, content type/length, selected server/diagnostic headers and
Retry-After are retained; cookies and authorization headers are excluded.
The request identity and fixed URL remain unchanged. Recognizing HTML does not
make RSS access reliable or establish why a security layer returned the page.

An unavailable response never enters the halt producer, establishes a startup
baseline, clears the last good state, means no halts, withdraws a request or
generates membership. A later valid snapshot resumes ordinary processing.
Progress distinguishes attempts from valid captures. Any recovered gap remains
an independent feed warning and unknown discovery interval. A transition with
such a gap is PASS_WITH_WARNINGS; without a new event it remains READINESS_ONLY
with that warning. A stop condition is FAILED/UNQUALIFIED even if a partial
journal audit passes. Partial-run audits use the actual observation end, rather
than counting future slots as missing. A gap never justifies backfilled quotes.

Fresh runs live beneath this package's `runs/nasdaq_shadow/<unique-run>`.
Retain the feed XML/snapshots, exact directory inputs, resolution replies, gate,
request/producer/publication/acquisition ledgers, quote journal, worker consoles,
status and call/commit receipts, `report.json`, `summary.txt`, and `result.json`.
They contain real provider data in live mode and must remain private. Public
source/validation contains generic code and synthetic evidence only.

The report separates feed acquisition and first discovery from request receipt,
actual publication, membership-effective time, first eligible and first observed
Hot slot, actual provider return, assembled response and journal-commit observer
receipt. Commit observer time is sampled immediately after the native journal
write returns; it is not an invented SQLite transaction timestamp. End-to-end
latency remains measured evidence, not a promised two/four-second bound.
RSS publication and once-per-minute acquisition add discovery delay before
request/Coordinator processing. Two-second Hot polling starts only after
membership is effective and does not make halt discovery a two-second service.
The minute interval does not guarantee access or explain the security decision.

The selected event's feed observations retain the original halt identity,
reason, reported resumption date/quote/trade fields (ET), acquisition time and
adapter receipt time. Trade-resumption status is either REPORTED_BY_FEED or
NOT_REPORTED_IN_CAPTURED_FEED. A reported schedule is not an observed executed
trade, and a blank field or disappearance proves neither continued halt nor
resumption. Quote and trade fields remain distinct. No automatic five-minute
release prediction, request withdrawal or resumption-triggered stop is added.

Acceptance requires complete scheduled shadow coverage, slot-bound membership,
native guard/ledger checks, source gate/ACK reconstruction, structural audit and
two identical acquisition-aware replays. Explicit invalid Hot observations can
produce PASS_WITH_WARNINGS if collection and transport pass, preserving availability
findings independently. No previous FAILED provider report is edited or reassessed.
An actual new qualifying live event, admission, publication and Hot acquisition
are required for LIVE_TRANSITION. No qualifying new event (or none resolvable
within these bounds) yields READINESS_ONLY, never that transition milestone.
The short synthetic command proves wiring only, even if it passes.

This does not establish full-day Schwab capacity, gap-free live restart,
production identity/authority/policy defaults, main readiness or live Overlay
integration. The identified duplicate cold reconstruction is unchanged.
