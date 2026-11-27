# Controller-owned Real Time OHLCV — experimental integration (2026-11-25)

**November 27 update:** the operator demonstrated live candle/volume display
and selection handoffs for MSFT, NVDA and ZYN. The accepted acquisition policy
is now **five attempts per rolling 60 seconds**, retaining one attempt per
symbol per completed five-minute candle interval. Detailed SDK retry metrics
are deferred. The qualification notes below record the original November 25
checkpoint; current evidence and rollout guidance are in
`Overlay_Unified_Release_Plan_2026-11-24.md`.

## Initial status and qualification boundary (November 25)

**Experiment only.** Source lives on
`experiment/overlay-unified-release-20261124`. Nothing in this
checkpoint updates `main`, the installed daily controller, existing
production workers, credentials, token files, or quote journals.

The previous 149-test Overlay suite passed on Windows 11 MasterBot on
November 25. New work below adds the **controller integration**; its
offline and desktop qualification is still pending. Do **not** launch
this candidate against a live Schwab session until separately approved.

## End-to-end design

1. A **future experimental daily controller launch** explicitly opts in
   via `--overlay-ohlcv`. The default `plan` and `start` paths do
   not enable the role. The authoritative daily plan records the
   selection root, independent candle evidence root, and a
   five-attempt budget with an explicit 60-second rolling window. The exchange calendar
   supplies actual open/close ET times.
2. The daily owner launches a child role named `ohlcv` **only after**
   the opening r1 is published, and before the exchange close. The
   existing private password pipe and `prepared_client(...auth_lock)`
   establish its authenticated Schwab client, just as for the other
   owned roles. The extra child has its own explicit lease, owner
   heartbeat checks, provider 401/403/429 backoff, status report,
   stop/exit handling, and optional explicit restart.
3. A Real Time Overlay can opt into **nonsecret chart-symbol selection**
   with both `--real-time-ohlcv-root` and
   `--enable-ohlcv-selection`. It requires a matching journal/day,
   loopback host `127.0.0.1`, a current daily owner and a daily plan
   with the OHLCV role explicitly enabled. This permission is
   independent of manual Membership promotion/demotion.
4. Selecting a symbol writes only `daily_session/DATE/ohlcv/selected.json`,
   with exact day, live owner PID, original opening identity, symbol and
   timestamp. No password, token, quote request or membership mutation
   occurs in the UI. The first Real Time refresh can also request the
   initial selected chart symbol once the session is accepting requests.
5. The owned OHLCV child reads that request, validates session/owner/
   original opening, and calls the already-qualified
   `OwnedOHLCVSchedule.tick` at most once for each selected symbol at
   each completed five-minute interval. A reserved durable nonsecret
   ledger limits duplicate requests across worker restarts; at most five
   acquisition attempts can be reserved in any rolling 60 seconds, including
   unsuccessful attempts and regular selected-symbol refreshes. The allowance
   does not reset at a clock-minute or candle boundary. SDK-internal retries
   are not individually counted by this application-level limit. Provider
   failures do not create an additional application retry loop within the slot.
6. After a successful Schwab `price_history` response the child
   publishes a self-contained original raw response and
   completed-only, source-backed five-minute OHLCV cache into
   `output/overlay_ohlcv_live/DATE/SYMBOL/ACQUIRED-HASH/` outside
   the production quote journal. The established read-only Real Time
   OHLCV reader discovers it on the existing five-second refresh and
   uses the same Historical Plotly chart, including **uniform blue
   volume bars** `#6fc8ff`.
7. A controller without `--overlay-ohlcv` remains unchanged.
   A viewer without `--enable-ohlcv-selection` is still read-only;
   even when enabled, the viewer **never calls Schwab**.

## Initial validation checklist (November 25)

- New offline test module
  `tests/test_overlay_ohlcv_worker.py` checks an explicitly enabled
  plan, nonsecret selected-symbol handoff, replaced controller identity,
  session stop, malformed selection, two successive completed candle
  captures and persisted service status, plus 429 backoff.
- **157-test** Overlay workspace offline suite expected after the new
  worker integration; actual MasterBot run pending.
- Must still qualify the complete **real SDK end-to-end flow** with
  independently authorized Schwab testing, including 5-minute
  response timing/latency, exchange early closes, token behavior,
  missing/late provider candles and any revised earlier candles.
- Actual production collector must not be replaced, patched or
  restarted while today's session is collecting. Real live activation
  requires a separate authorization and a future session-specific plan.

## Initial milestone deferrals (November 25)

Custom indicators, historical backtesting, OHLCV across all Uni
members, tier-list refinements, trade execution and any second
uncontrolled Schwab poller. These are not part of this milestone.
