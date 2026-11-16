# Overlay workspace layout and mode selector — experimental prototype (2026-11-16)

This is an **experimental, read-only UI layout change**, based on Dan's
activity-bar / side-panel / tabbed-workspace illustration. It does not change
the daily collector, production journal, replay timing, or GitHub `main`.

## Workspace organization

The **workspace-wide mode selector** sits directly above the five main tabs,
independent of the activity side panel:

| Mode | Visible status in this prototype | Intended evidence |
| --- | --- | --- |
| Real Time | Disabled; not implemented | Committed live records and separately captured candles, without interrupting collection |
| Historical | Selected and operational; read-only | One day's journal replay and validated historical OHLCV |
| Backtest | Disabled; not implemented | Versioned strategy run, historical inputs, and explicitly simulated executions |

The selected mode will ultimately supply the context for **all** tabs, not just
Charts. This avoids separate chart implementations or a Live/Historical split
inside the Charts tab. Changing from Historical to another mode is impossible
in the current prototype; there is no synthetic live feed, backtest execution,
or callback pretending otherwise. A future adapter must provide the mode's
session identity, provenance, clock semantics, and available features before
that option can be enabled. Historical clock and chart behavior is unchanged.

The left activity bar selects one independently scrollable side-panel section:

- **Information** — session identity and replay evidence timing.
- **Graph Setups** — the existing detailed-hover, price/percent, clicked-candle
  field, 0% reference, and per-channel point size/opacity controls.
- **Time Controls** — the existing historical replay play/pause, step,
  restart, seek, speed, and position controls. Its internal activity ID remains
  `activity-replay` to preserve callback behavior. Mode-specific controls can
  be added here as the other data paths qualify.
- **Backtest Settings** — inert description; no backtest implementation.
- **Trading** — inert description; no order execution or simulation.

The main tabs are **Membership**, **Charts**, **Indicators**, **Monitor**, and
**Alerts**. Membership holds the existing nested-channel cards and current
symbols table. Charts uses the existing observation figure, including candles,
volume, quote points, membership bands, and independent-scale navigator when a
validated OHLCV cache is supplied. Monitor holds replay totals and the optional
independent acquisition-health reader. Indicators and Alerts remain honest,
inactive future-work placeholders; they never invent evidence.

The mode selector, activity rail, and main tabs are independent. The side panel is horizontally
resizable on wider screens; the app stacks the tab workspace below it on narrow
screens. Existing light/dark theme, zoom, reference selection, replay and
server-stop callbacks keep their IDs and behavior.

The mockup's close (`×`) action for a main tab is **not implemented** in this
first layout; a tab remains selectable and cannot be accidentally destroyed.

## Offline verification

`verify_overlay_workspace.cmd` is the experimental workbench verifier for
Windows. Activate the existing `dashing` environment in a **separate checkout**
of this experiment branch before running it. It runs the 41 accepted Overlay
projection/rendering tests plus 14 dashboard and layout tests (55 total),
without real provider calls or credential access. It reports its Dash and Plotly
versions and explicitly labels visual inspection as pending.

Initial Linux development verification: **55/55 PASS**, Python 3.12, Dash
4.5.0, Plotly 7.1.0, no skipped tests. The additional dashboard replay/probe
suite passed **25/25**. Dan accepted the original activity-rail layout during
an interactive preview on MasterBot (`dashing`: Dash 2.18.2, Plotly 6.5.2;
55 tests PASS). The later workspace mode selector still needs its own desktop
visual review before this revision is considered qualified.

For an interactive **synthetic, offline** preview, in the same experimental
checkout run `call preview_overlay_workspace.cmd` from `dashing`. Open the
printed localhost address, confirm that **Historical** is selected and that
**Real Time** and **Backtest** cannot be selected, click **Charts**, and try the
activity icons for Graph Setups and Time Controls. The preview does not open any SQLite journal,
request credentials, or make provider calls. Stop with Ctrl+C or the dashboard's
Stop server button. Do not infer desktop visual acceptance from the unit tests.

The old `verify_overlay.cmd` still strictly checks the immutable 117-file
integration fingerprint from PR #9. It is **expected to BLOCK** on legitimate
new Overlay changes; do not rewrite those accepted source hashes or relabel
this prototype as identical to the 2026-11-13 integration checkpoint.

## Working with November 10–13 evidence

The historical dashboard can point directly at a retained SQLite journal; it
does not require copying the journal into the source-code checkout. Its existing
`--observation-overlay-cache` argument is required for the historical **Charts**
tab to display actual five-minute OHLCV. These quote journals alone must not
be used to fabricate candles or volumes. A later task will connect the new
daily-session `plan.json` / Nasdaq event records to the historical workflow
with explicit provenance and causal timing.

Nothing here modifies the `one-unlock-daily-flow-v1` production controller,
the prepared November 16 r0, or the original experimental session evidence.
