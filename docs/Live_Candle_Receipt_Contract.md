# Live candle receipt increment — October 9, 2026

This experimental branch adds one-symbol five-minute candle receipts to the
isolated live Overlay prototype. It does not install into production, acquire
quotes, change membership, or attach to the running production journal.

## Evidence and visibility

The separate `live_candle_capture_v1` producer preserves each response body and
its SHA-256 digest, request dispatch time, response receipt time, and outcome.
Its `request_plan.json` identifies the symbol, session and requested interval.
The reader consumes complete `sample_NNNN.receipt.json` files. An orphan raw
response without a receipt is not a committed input.

Every response remains immutable. Equal successive values keep the same observed
value-version number but still retain their individual receipts. Different OHLCV
values create another observed version. Version numbers are local to one capture,
not provider sequence numbers. Source ordering uses receipt time and sample number;
regressing receipt times within a capture are refused.

At an inclusive inspection cutoff, the latest known observation for each candle
is selected from receipts at or before that cutoff. A candlestick is displayed
only if that selected observation was received at or after the interval ended.
An observation received while the interval was open is held separately; passing
the end time cannot convert its values into a completed observation. Pending
counts include intervals awaiting a closing receipt, even after their end time.

Subsequent closed-interval corrections appear at their own receipt times. Seek
backward restores earlier known values. Return to Live reconstructs the same view
as a fresh process at the same cutoff after discovery catches up. Step includes
candle receipt times in addition to quote and membership availability times.
Selected percentage references carry the clicked candle observation's receipt
time, preventing a reference from leaking backward before it was known.

The dashboard labels its inspection cutoff **Viewing time** in the playback
controls, header and chart. It updates even when no new quote or membership event
is projected. This differs from the latest journal event time, which may stay
unchanged while candle receipts arrive. The full ET timestamp wraps instead of
being truncated. Seek includes the entered second; Step advances to the next
available quote, membership change or candle receipt.

The first SPY capture ran 14:18:49–14:30:49 ET on October 9. All 49 responses and
their hashes were verified. An unfinished candle was returned in every response.
The 14:15, 14:20 and 14:25 intervals first appeared closed in observations received
at 14:20:04.166912, 14:25:04.170852 and 14:30:04.189309 ET. Their values remained
unchanged in subsequent observed closed-interval responses. This bounds our
observations at a 15-second cadence; it does not establish exact publication
latency, provider finality, a correction horizon, or behavior for other symbols.

## Separation from historical caches

The existing completed-session cache remains a retrospective diagnostic input
whose historical visibility is simulated at interval end. It is not proof those
values were available live at that time. A captured-receipt view cannot mix that
cache with the captured versions. The chart heading, counts and hovers identify
captured receipts; closed intervals are never labelled final.

## Source failures and bounded discovery

The reader loads at most a configured number of new receipt files per refresh
(32 by default), and stages an entire page before making it visible. Invalid
JSON, missing bodies, incorrect hashes, malformed candles, conflicting sample
numbers, changed plans and changed discovered evidence fail visibly. Previously
validated versions are retained. Quote discovery continues independently of a
candle-source error; neither pathway writes to the other source.

Existing receipt and raw-file stat changes trigger content revalidation, and
missing discovered files are refused. This assumes an append-only producer and
is not a general tamper-proof archive: a malicious writer restoring all file
metadata can defeat stat-based change detection. Fresh reconstruction validates
every raw body against its receipt. Replacing the capture directory while
following it is unsupported. A normal producer restart creates a new capture;
merging several capture directories into one ongoing source is future work.

The reader reports only receipts visible at the inspection cutoff, including
error receipts. A failed response does not erase the latest known good candle.
Missing candles remain missing; sampled quotes do not substitute for OHLCV.

## Review and acceptance

The synthetic demo demonstrates an unfinished candle, a closing receipt and an
intentional later correction alongside synthetic quotes and membership. The
saved-SPY demo reads the actual separate capture, uses a simulated clock and an
empty synthetic quote journal. It contains no real quote/membership evidence.
Neither demo requires an open market, a password or API access.

Local tests cover inclusive visibility, backward seek, pause with continuing
discovery, Step, fresh-process equality, malformed/conflicting evidence, guarded
paths, changed input, partial batch refusal, independent source failures, percent
reference timing and real Dash callbacks. Native Windows checks and browser
review are still required. A controlled live quote-journal attachment remains a
separate acceptance task; production opening paths are excluded by this launcher.

This increment does not implement live halt capture, manual membership requests,
coordinator integration, Hot policy, automatic recovery or continuous candle
producer supervision. The existing bounded capture probe supplies the evidence.

## Single-candle rendering and playback counts

Candle body widths use five-minute spacing even when just one candle is visible
or visible candles have intervening gaps. For Plotly's candlestick renderer, an
empty spacing point one interval before the first candle has null OHLC and no
custom data. It is a layout aid only: it creates no candle, receipt, price, hover
selection or evidence count. Volume bars have an explicit interval-based width.

The playback counter labels journal events (membership/quotes) separately from
candle responses. The progress bar includes both discovered event populations.
A candle-only Step therefore advances the response count and progress even if
the journal-event count is unchanged. One response can contain multiple candles;
Step advances to an availability time, not to an individual candle interval.

## Console shutdown acceptance

The first native Windows verification on October 10 reached the synthetic HTTP
server but failed in the event helper's AttachConsole call with WinError 5, before
an interrupt was sent. This was a test-harness omission, not a viewer Ctrl+C
result. The corrected helper detaches itself first, attaches to the exact owned
child PID, and verifies that only the child and helper occupy that console before
sending the event. It refuses unknown console members or an unreadable process
list. Application shutdown behavior was unchanged by that harness correction.

The next native verification successfully emitted the event but the owned viewer
did not exit within eight seconds. This is a failed shutdown result, not native
acceptance. The subsequent correction adds a scoped Win32 console-control handler
as well as the Python handlers. It explicitly enables Ctrl+C in the viewer process
and registers its callback after existing handlers. The callback consumes only
Ctrl+C/Ctrl+Break and records a reason; the main loop closes the HTTP listener.
Windows close, logoff and shutdown events retain their normal handler processing.

The Win32 callback stays alive until successful unregistration. Failed removal
retains it and reports the cleanup error. The previous Python handlers are restored
exactly. A known previous Python ignore-Ctrl+C policy is restored as well; Windows
does not expose a getter for an otherwise unknown inherited ignore attribute, so
the viewer does not claim to reconstruct that unknown attribute. None of these
changes detach the viewer or modify another process's console settings.

The owned-console test now reports setup, event receipt in the main loop, handler
restoration and listener closure. It checks ordinary Python ignore, explicit
Windows ignore and an older consuming handler in separate owned child consoles.
This distinguishes an unreceived event from cleanup that stalls after receipt.
These native tests and the physical-key checks still require Windows acceptance.

October 9 Windows review confirmed the browser's Stop server control but found
that Ctrl+C did not stop the isolated candle viewer. Candle visibility and
playback acceptance remain valid; console shutdown is a separate requirement.

The viewer now temporarily installs explicit SIGINT/SIGBREAK handlers on its
main thread and restores the previous handlers after cleanup. Handlers only
record a stop reason; they do not acquire locks. A short main-thread polling
loop also accepts Q or a raw Ctrl+C key from an interactive Windows console.
Extended console-key scan codes do not count as Q. Worker-thread/embedded
servers do not alter process handlers or consume console input.

The browser stop, console signal and Q key close the same HTTP listener. Demo
viewers record their stop reason in a separate viewer_stop.json under their
new demo output folder. The original candle capture and production journals
are not modified. Clear any CMD text selection with Esc before keyboard tests.

A Windows-only test creates its own synthetic server and console, delivers a
real control event to that console alone and checks process exit and listener
closure. Manual Ctrl+C and Q acceptance in overlay-candles are still required.
The original October 9 console failure's precise cause is not established.
