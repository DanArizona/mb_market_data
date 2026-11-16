# Historical Overlay workspace layout — experimental prototype (2026-11-16)

This is an **experimental, read-only UI layout change**, based on Dan's
activity-bar / side-panel / tabbed-workspace illustration. It does not change
the daily collector, production journal, replay timing, or GitHub `main`.

## Workspace organization

The left activity bar selects one independently scrollable side-panel section:

- **Information** — session identity and replay evidence timing.
- **Graph Setups** — the existing detailed-hover, price/percent, clicked-candle
  field, 0% reference, and per-channel point size/opacity controls.
- **Replay Settings** — the existing play/pause, step, restart, seek, speed,
  and replay position controls.
- **Backtest Settings** — inert description; no backtest implementation.
- **Trading** — inert description; no order execution or simulation.

The main tabs are **Membership**, **Charts**, **Indicators**, **Monitor**, and
**Alerts**. Membership holds the existing nested-channel cards and current
symbols table. Charts uses the existing observation figure, including candles,
volume, quote points, membership bands, and independent-scale navigator when a
validated OHLCV cache is supplied. Monitor holds replay totals and the optional
independent acquisition-health reader. Indicators and Alerts remain honest,
inactive future-work placeholders; they never invent evidence.

The rail and main tabs are independent. The side panel is horizontally
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
suite passed **25/25**. The desktop layout has **not** yet received visual
acceptance on MasterBot; its `dashing` environment uses Plotly 6.5.2 and needs
an independent review before this UI branch is considered qualified.

For an interactive **synthetic, offline** preview, in the same experimental
checkout run `call preview_overlay_workspace.cmd` from `dashing`. Open the
printed localhost address, click **Charts**, and try the activity icons for
Graph Setups and Replay Settings. The preview does not open any SQLite journal,
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
