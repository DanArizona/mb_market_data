# Unified Overlay: bounded read-only daily attachment (November 24, 2026)

The historical workspace's symbol-selection path is real-journal verified on
MasterBot, including 100/100 isolated Windows tests, responsive indexed symbol
switching, and synchronized failed lookup feedback.

## Completed in experimental source

`overlay_session_catalog.inspect_session` validates an exact daily controller
plan against its matching schema-v2 hierarchy journal. The new
`overlay_live_attachment.ReadOnlySessionObserver` uses that validated
identity, SQLite `mode=ro` plus `PRAGMA query_only=ON`, and a short,
consistent read transaction. It reports the count and most recent timestamp of
committed acquisitions, effective membership revision count, and any increase
since its previous observation. It also verifies the latest committed batch's
observation cardinality. No production data is changed, and no provider,
credential, controller, poller, or operator action is touched.

The observer reports **saved controller phase and timestamp**, explicitly
`live_process_verified=false`. A saved `COLLECTING` phase is **not**
proof the producer is running, and a growing acquisition count is not a
channel-health or audit PASS. This is an attachment data boundary, not
completed Real Time UI.

On a MasterBot `dashing` experimental checkout, a bounded manual check is:

```cmd
python -u -B probes\inspect_overlay_live.py --market-data-root "%USERPROFILE%\dailyflow_qa\daily_flow_candidate_20261106\market_data" --date 2026-11-24
```

It reads the real journal **once**, exits, and makes no Schwab request.
Repeat once later to observe changes, but the one-shot CLI intentionally does
not claim liveness between calls. If the session has finished, the count may
legitimately be unchanged.

## Not yet qualified or enabled

- A continuous Real Time chart/membership data stream, current-session
  freshness UI, or Real Time workspace mode.
- Historical-to-Real-Time UI switching in one running browser session.
- Manual membership preview/confirm controls against the authoritative daily
  worker. The synthetic prototype's controls have **not** been promoted.
- Production installation or merge.

## Next implementation stage

Integrate a bounded committed-event tail into the unified Overlay in **read-only
Real Time** mode, with explicit as-of timestamps and no publication pathway.
Qualify one actual daily journal attachment (including viewer shutdown while
polling continues) before adding governed preview/confirm Membership actions.
The original production controller, credentials, and journals remain untouched.
