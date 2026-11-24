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

## Experimental Real Time viewer (unverified on MasterBot)

The *same* `dashboard_quote_observation_journal.py` executable now accepts
`--real-time-data-root`, with the current-day journal as its positional
argument. This mode validates the exact daily plan/journal identity and starts
an isolated localhost viewer with **Real Time · committed read-only** selected.

The viewer reads the established causal prefix once and subsequently refreshes
every five seconds. A committed-event tail appends only newly recorded complete
acquisitions and effective Membership revisions to the existing charts, tier
lists, and inspector. It does **not** rerun full-day acquisition replay on each
refresh, call Schwab, start or stop workers, publish Membership, or change any
production file. It rejects a newly arriving event that would have to be placed
before the already displayed causal prefix, rather than silently falsifying
time ordering.

Example on MasterBot (only on the **same ET session date**):

```cmd
python -u -B probes\dashboard_quote_observation_journal.py "%USERPROFILE%\dailyflow_qa\daily_flow_candidate_20261106\market_data\output\quote_observation_journal_v2_api_opening_2026-11-24\2026-11-24.sqlite3" --overlay-symbol NVDA --nasdaq-daily-events-json "%USERPROFILE%\dailyflow_qa\daily_flow_candidate_20261106\market_data\output\daily_session\2026-11-24\nasdaq\events.json" --real-time-data-root "%USERPROFILE%\dailyflow_qa\daily_flow_candidate_20261106\market_data" --host 127.0.0.1 --port 8051
```

Historical remains the default **without** the attachment flag. This beta
requires a viewer restart to switch modes; a seamless in-browser
Historical/Real Time switch is not yet implemented. Backtest and all operator
actions remain disabled. The live heading shows latest committed evidence,
the saved controller phase, and explicitly warns that process liveness was
not independently verified. Do not infer polling health or audit PASS from
a growing count.

**Status:** experimental implementation only. Offline suite expanded to
109 expected checks; no real-day in-browser live qualification yet. Confirm
the 2026-11-24 session journal is read-only-attachable, demonstrate additional
acquisitions and refreshed quote/Hot/Membership evidence while collectors
continue, and stop only the viewer before treating this as qualified.

## Not yet qualified or enabled

- Real Time chart and Membership refresh still require Windows qualification
  against the active production journal; the current beta is not a release.
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
