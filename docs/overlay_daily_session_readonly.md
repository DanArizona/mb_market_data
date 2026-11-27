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
known at the earlier observation.

### Halt lines and first-observed diamonds (restored November 27)

Each observed episode also has a 3.5-pixel magenta horizontal line. Once
the halt has been observed and an in-halt quote is available, the line begins
at Nasdaq's reported halt time, including the interval before our first
observation. Its fixed price is the first available journal quote within the
halt. It renders above polling points. One **Nasdaq halts** Plotly legend entry
hides/restores all horizontal lines together, independently of the diamond
series. Each episode retains its own timestamps and reason in hover.
The hidden-layer choice survives new episodes and replay frames with no lines.

The line extends only through the displayed evidence clock until a reported
trade-resumption endpoint is known. Then it ends at that reported time, still
clipped to the display clock. Hover distinguishes a reported endpoint from
"not yet reported" and gives the separate receipt times. Resumption updates
never become visible before their own receipt, including during backward seek.
Neither flat prices nor a five-minute timer manufacture a halt or its ending.
In particular, the line's price is the selected journal quote price, not a
Nasdaq pause-threshold value. Earlier polling observations can already have
that same last price. Detailed hover's **Last trade**, **Trade age**,
**Cumulative volume**, and **Security status** help distinguish repeated
prices from a provider-reported halted status. Quote dots use acquisition
completion times; the line uses Nasdaq's reported halt-event timestamp.

The daily source now preserves endpoint revisions directly. Existing sessions
recover their receipt history from retained `nasdaq/captures/*.json` files;
when captures are absent, `last_observed_at` is a conservative bound for the
latest endpoint. Keep the capture directory alongside `events.json` for the
most accurate historical timing.

In Real Time mode, `--real-time-data-root` automatically attaches the matching
`output/daily_session/DATE/nasdaq/events.json`. An explicit
`--nasdaq-daily-events-json` overrides that path; an explicit historical
`--nasdaq-halts-json` retains its fixed-source behavior. The daily file is
refreshed while the symbol stays selected, even before candles are available.
A missing file can arrive later without restarting the viewer. Missing,
invalid, or truncated updates report their condition and retain previously
accepted evidence; they do not stop independent quote/candle refreshes.

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

## Stage 3 — separate Membership exploration from Monitor

The Membership tab now provides three independently filtered, scrollable
lists: **Uni only**, **Focus only**, and **Hot**. Each symbol is shown exactly
once in its highest current tier for easy inspection, while the underlying
replay membership remains nested (`Hot ⊆ Focus ⊆ Uni`). Selecting any row
opens a detail inspector based exclusively on saved evidence available at the
current replay time. Latest quote price, reference change, range, volume,
security status, and an approximate market cap are shown when the journal
supplies the required fields; otherwise they are explicitly unavailable.
Selection is exclusive across all three lists: clicking a new symbol clears
the previously highlighted row, even if it belonged to a different tier.
The tier headings and inspector badge use the matching Uni cyan, Focus amber,
and Hot magenta channel colors.
An ordinary demotion does not imply a rest-of-day automatic promotion block;
the journal does not record that policy state, so the inspector says unknown.

The Monitor tab now contains the former membership summary cards and complete
filterable observation table, alongside replay totals and acquisition health.
The summary is compact so the table fits better on a 1080p-height screen.
This move preserves the replay-time and wall-clock distinction. Operational
health files are optional for completed-day replay: the default lookup is
relative to the **journal's** output directory, not to the experimental
viewer checkout. If no matching health report exists, the page shows a small
"not attached" note; use `--polling-health-directory` to supply an explicit
historical health folder if available. The absent report does not affect
saved quote replay.

**No drag-and-drop, manual membership writes, or live trading controls are
enabled.** Historical and Backtest workflows remain separate, and this
experimental viewer has no authority to publish membership commands.

## Safety and qualification

- Historical snapshot is read-only. Stop this viewer without stopping collectors.
- Reported trade-resumption updates use their own receipt times; a later
  `CURRENT` or `HISTORICAL` record cannot reveal an endpoint earlier in replay.
- Current experimental presentation does not qualify real-time or backtest adapters.
- The original `INTEGRATION_SOURCE_INVENTORY.json` remains an immutable
  provenance fingerprint. Run `verify_overlay_workspace.py` for tests of the
  experimental source instead of using the original integration fingerprint
  to judge intentional Overlay changes.
