# Read-only Overlay: completed daily-flow session

Experimental Overlay development only. Do not run this from an active polling
controller directory or change the production launch procedure. The daily-flow
session journal and Nasdaq capture are inputs; this viewer does not write them
or make Schwab provider requests.

## Stage 1 — show saved quotes, membership, and Nasdaq reports

In a separate Windows CMD window on MasterBot, activate the existing `dashing`
environment and change to the updated experimental Overlay checkout. Launch
the historical view with this single command (November 13, 2026, `ONFO`):

```cmd
python -u -B probes\dashboard_quote_observation_journal.py "%USERPROFILE%\dailyflow_qa\daily_flow_candidate_20261106\market_data\output\quote_observation_journal_v2_api_opening_2026-11-13\2026-11-13.sqlite3" --overlay-symbol ONFO --nasdaq-daily-events-json "%USERPROFILE%\dailyflow_qa\daily_flow_candidate_20261106\market_data\output\daily_session\2026-11-13\nasdaq\events.json" --port 8051
```

Open the local address printed by the viewer. It initially shows the completed
day so that real quote observations are immediately visible; **Restart**
returns to the beginning for interactive historical replay. The Charts tab is
the selected-symbol Observation Overlay. With no OHLCV cache, the candle and
volume layers are intentionally absent; quotes and membership are real data.

For the November 13 ONFO session, six `LUDP` episodes were recorded in
`nasdaq/events.json`. Their `halt_time` is a reported market-event timestamp;
`first_observed_at` is when the system learned of the report. Daily events
appear only after that first observation. A diamond marker uses a saved quote
price for visual placement and reveals its quote timestamp on hover. The
daily report's later `resumption_trade_time` is **not** treated as a release
known at the earlier observation. No duration is inferred from these daily
reports.

The November 13 journal remains audit **FAIL** because six Hot slots are
missing. It remains useful for forensics of the observations that *were*
recorded; the viewer must not imply missing slots were collected.

## Stage 2 — add genuine five-minute Schwab candles

The existing `probes/acquire_observation_overlay_ohlcv.py` acquires a single
completed historical session for one symbol with `frequency=5` and extended
hours. It requires the existing secure Schwab client and prompts separately
for the encrypted configuration password. Run it only after confirming that
the session has completed, from an environment where `mb_tools.schwab_secure`
is installed. Supply `ONFO`, `2026-11-13`, an explicit encrypted-config path,
and a separate output root **outside production journal directories**.

The acquisition writes immutable raw response evidence, a validated cache,
and a manifest; it does not modify the journal. After successful acquisition,
rerun the Stage 1 viewer with `--observation-overlay-cache` pointing to the
exact `OO cache` file reported by that acquisition. This enables completed
five-minute OHLCV candles and volume alongside the same quote, membership,
and Nasdaq-event evidence. If the provider returns no usable candles, keep
the quote-only view; never fabricate OHLCV.

## Safety and qualification

- Historical snapshot is read-only. Stop this viewer without stopping collectors.
- No manual release can be inferred from a `CURRENT` Nasdaq final record.
- Current experimental presentation does not qualify real-time or backtest adapters.
- The original `INTEGRATION_SOURCE_INVENTORY.json` remains an immutable
  provenance fingerprint. Run `verify_overlay_workspace.py` for tests of the
  experimental source instead of using the original integration fingerprint
  to judge intentional Overlay changes.
