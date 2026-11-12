# Daily flow: shared membership and live Overlay connection

This is a separate integration candidate for the accepted three-channel daily
flow. It does not install over `dailyflow_three_qa`, change existing data, or
activate itself. Keep the Windows-accepted three-channel package as the fallback.

## Delivered scope

* Existing one-attended-unlock startup, 09:25 current-volume top 40 selection,
  retained top 100/full ranking, interleaved Uni/Focus/Hot collectors, independent
  desktop alerts, and automatic post-close r0 build/publication remain present.
* An optional **single membership worker** accepts manual commands and Nasdaq
  halt requests. It uses the existing native atomic hierarchy publisher. The
  pollers keep their own processes and resolve effective membership each slot.
* Manual promotion supports **Uni**, **Focus (plus Uni)**, and **Hot (plus both)**.
  Ordinary symbol-level demotion to Focus or Uni overrides the existing Nasdaq
  episode, including duplicate reports and release updates. A halt timestamp
  later than that demotion can request Hot again. This state survives restart.
* A separate, optional, credential-free Nasdaq worker reuses the existing RSS
  parser/downloader. CURRENT and same-day HISTORY alternate at one total request
  per minute. LUDP/M events use symbol + original halt timestamp as identity.
* A read-only live adapter uses the **existing Observation Overlay figure** for
  saved quotes, effective membership transitions, and observed halt/resumption
  records. It has no market-data client, credentials, or journal write access.
  This is a quote-only connection, not the full old dashboard or a new candle
  acquisition service. Existing historical candle/replay modes remain available.

No order placement, account transaction, or trading execution is implemented.

## Important boundaries

The older standalone Hot coordinator must NOT run beside this candidate on the
same journal. The new daily membership worker is the sole intraday publisher.
It is a thin native-journal adapter, not a claim that every legacy coordinator
policy/UI has been ported. No persistent "Suppress Automatic", GUI membership
editor, or arbitrary removal of symbols from Uni is included.

The opening ranked forty and ranking input never change because of intraday
promotions. Initial MSFT/INTC/NVDA seeds use the existing operator-test path.
Seeds count toward the normal capacity: at most **40 active Hot members** and
**40 distinct outside-opening-Uni additions in the session**. A demotion frees
an active Hot place, but does not reset the distinct-symbol allowance. A manual
request for an outside-Uni symbol uses an exact, equity-type broker quote
confirmation; previously verified symbols reuse that evidence. This does not
claim a new full symbol-directory identity framework.

Commands are local, same-user controls, not an authenticated remote service.
The CLI queues commands; it never edits a journal directly or asks for a password.
Requests from a prior controller invocation are rejected. Accepted requests and
restart state are committed in the same native membership revision; the receipt
is recoverable if the process stops after committing but before acknowledging.
Effective times are normally three seconds after publication preparation, not
backdated to the operator click or the halt time. Already-started acquisitions
retain the membership they resolved before the change.

Nasdaq startup ignores episodes older than its persisted first-start cutoff.
A first launch after the open therefore does not retroactively promote earlier
halts. The cutoff is preserved on restart. Feed updates arriving after a manual
demotion cannot resurrect the same/older halt. Source outages are shown as stale;
they do not clear membership or stop manual controls or quote collection.
Capture evidence is retained. This is best-effort discovery, not a claim to
capture every intermediate feed mutation or to guarantee a detection deadline.
There is no fabricated live halt input command.

## Windows offline verification — first action

Extract into a NEW folder, for example:

```
C:\Users\danla\dailyflow_membership_qa\daily_flow_membership_20261110
```

Do not change the existing fallback folder or copy its output into this package.
In `closeout-qa`, run `verify_offline.cmd` by its full path. The core verification
needs the same installed packages as the accepted three-channel candidate; it
does **not** need Plotly or Dash and does not install anything. It checks source
hashes, blocks network/interactive password access, and uses temporary journals
and synthetic provider responses. Expect **304 tests** (246 previous + 58 new).
Logs and a real-three-child membership proof are under this package's `output`.

The actual-process proof demotes INTC to Focus and promotes it back while the
same Uni/Focus/Hot children keep running. A synthetic Nasdaq-source promotion
adds a second symbol; native audit and two replays verify the resulting journal.
The proof uses coordinated synthetic clocks/providers, not a live Nasdaq feed.

### Separate read-only chart verification

Use the existing Plotly environment (normally `dashing`), not a dependency change
in the collector environment:

```cmd
conda activate dashing
call "%USERPROFILE%\dailyflow_membership_qa\daily_flow_membership_20261110\verify_overlay.cmd"
```

This runs **41 figure/adapter regressions**, including the original Overlay
rendering tests, and writes a self-contained synthetic HTML chart preview. It
requires Plotly but not Dash, credentials, `mb_tools`, or a provider connection.
A missing Plotly import is reported explicitly; no automatic installation occurs.
The preview tests figure creation, not the visual appearance on MasterBot.

## Plan review — after Windows verification

Keep the original data location so the prepared r0 and historical evidence stay
where they are. No data migration is necessary. For the prepared November 10
session, the read-only plan command is:

```cmd
conda activate closeout-qa
set "DF_MEM_CODE=%USERPROFILE%\dailyflow_membership_qa\daily_flow_membership_20261110"
set "DF_DATA=%USERPROFILE%\dailyflow_qa\daily_flow_candidate_20261106\market_data"
call "%DF_MEM_CODE%\daily.cmd" plan --opening-proposal "%DF_DATA%\output\daily_universe_production\2026-11-10-from-2026-11-09\opening_hierarchy_r0.json" --market-data-root "%DF_DATA%" --ecfg "%USERPROFILE%\MBV\secure_schwabdev.ecfg" --hot-symbols MSFT INTC NVDA --membership-controls --nasdaq-halts
```

`--membership-controls` enables the shared writer; `--nasdaq-halts` additionally
enables the paced source. Without these flags, the three-channel behavior is
retained. No live startup is performed by plan or verification. Review the plan
before choosing a live package. Never launch it alongside the fallback session.
Do not replace code underneath a running controller or collector.

The live launch uses `start --live` instead of `plan` with the same arguments.
It prompts once in the foreground and retains the existing session credential
handoff. This documentation is not a report that that live launch has succeeded.
The writer starts after the opening selection/seed publication. If Focus selection
was unavailable, this increment does not implement a late-ranking recovery mode.

## Manual controls — once the integrated session is running

Use a separate CMD window; keep the live controller running. Example variables:

```cmd
conda activate closeout-qa
set "DF_MEM_CODE=%USERPROFILE%\dailyflow_membership_qa\daily_flow_membership_20261110"
set "DF_STATE=%USERPROFILE%\dailyflow_qa\daily_flow_candidate_20261106\market_data\output\daily_session\2026-11-10"
```

Show latest published membership (future-effective state is explicitly marked):

```cmd
call "%DF_MEM_CODE%\membership.cmd" members --state-root "%DF_STATE%"
```

Demote the test symbol without stopping its collectors:

```cmd
call "%DF_MEM_CODE%\membership.cmd" demote --state-root "%DF_STATE%" --symbol INTC --to focus
```

Promote it back:

```cmd
call "%DF_MEM_CODE%\membership.cmd" promote --state-root "%DF_STATE%" --symbol INTC --to hot
```

For direct Uni addition use `promote --symbol SYMBOL --to uni`; Focus uses
`--to focus`. Substitute a real intended symbol, not the literal word SYMBOL.
Commands print their ID and wait up to 15 seconds for publication/effectiveness
feedback. A slow request may remain PENDING; do not submit duplicates. Use
`membership.cmd result --state-root ... --id <displayed-ID>` to inspect it later.
`EFFECTIVE_AS_PUBLISHED` is not proof that another later command did not change
membership; use `members` or the view to inspect current effective membership.
New-symbol transient lookups have bounded retries; invalid symbols are rejected.
No result is reported as effective merely because a request file was created.

## Read-only live viewer

In a separate `dashing` window (or another environment already containing Plotly):

```cmd
conda activate dashing
call "%USERPROFILE%\dailyflow_membership_qa\daily_flow_membership_20261110\overlay.cmd" --state-root "%USERPROFILE%\dailyflow_qa\daily_flow_candidate_20261106\market_data\output\daily_session\2026-11-10" --symbol INTC
```

Open the localhost URL printed by the server (default port 8053). The viewer
shows the original Overlay figure, dynamic symbol choices, pause/return-to-live,
and expandable health/membership/alert details. Its default bounded point window
is the latest 12,000 saved observations for the selected symbol; truncation is
labeled and the full journal is retained. No OHLCV candles are fabricated.

Closing the browser or pressing Ctrl+C in this viewer's console stops only the
viewer. Never use the controller's Exit session command to close the chart.
This reader uses short SQLite read-only transactions and has no write endpoints.
It binds to localhost and serves Plotly JavaScript locally, without a CDN.
A view read failure preserves the previous chart and prominently marks it stale.

## Evidence and limitations

`LOCAL_VALIDATION.json` describes local development evidence. Windows, actual
browser rendering, live source admission, live identity requests, and sustained
three-channel uptime must be demonstrated separately. The delivered 246-test
baseline already had Windows acceptance and the desktop alarm demonstration;
that prior evidence does not automatically qualify these changed connections.
Local tests use synthetic credentials and provider responses. No real vault,
production journal, or installed package is modified while developing this copy.

Explicit restart controls exist for the membership and Nasdaq workers:
`daily.cmd restart-membership --state-root ...` and
`daily.cmd restart-nasdaq --state-root ...`. They retain the active controller,
do not require another password, and do not restart collectors. Requests are not
proof of successful recovery: confirm advancing status afterward. Unexpected
service failures are visible to the independent health inspector/watchdog.
