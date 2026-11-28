# ZYN halt timing investigation and MasterBot clock correction

For the reusable command guide, see the
[MasterBot timing runbook](MasterBot_Timing_Runbook_20261127.md). This document retains
the incident evidence and its qualifications.

## Outcome and scope

The November 27 investigation found a current MasterBot clock lag of about
2.53 seconds, corrected it through the existing Windows Time configuration,
and verified that Windows Time is configured for automatic startup and is
running. This strongly corroborates a local-clock explanation for saved quote
observations appearing before Nasdaq's reported halt start.

The evidence below is operator-supplied output from the saved-data probe and
Administrator CMD on MasterBot. The investigator did not directly access the
Windows host or its full journal. Market-event times below are Eastern;
Windows diagnostic times are retained as displayed, without assuming the
Windows desktop's local timezone.

**Qualification:** the evening clock measurements do not establish the exact
clock offset at 11:59 ET or throughout the session. Preserve the original
journal timestamps; do not retrospectively shift them by the evening offset.
Automatic startup is verified by configuration readback, not by a new reboot.
Neither an immediate reboot nor another live-market test is a development gate.

## Saved halt and quote evidence

The read-only `probes/inspect_halt_timing.py` probe was published in commit
`c7f856c99bad72a3f3d07340a0ba6d317aae5977`. It joins saved quote observations
to acquisitions and reads the corresponding daily Nasdaq event and first
immutable capture. It opens SQLite in read-only mode and creates no provider
client. Its default inspection window is 35 seconds before and 15 seconds
after the selected halt.

For ZYN on November 27:

| Field | Recorded value |
| --- | --- |
| Nasdaq episode key | `ZYN@2026-11-27T16:59:20.066000+00:00` |
| Nasdaq halt start | **11:59:20.066 ET** |
| Nasdaq reason | `LUDP` |
| First Nasdaq observation | **12:01:04.838426 ET** |
| First capture | `170104838426-f872.json`, mode `CURRENT` |
| Capture raw-XML hash check | Matches retained `raw_sha256` |
| Preceding halt start | 11:53:53.476 ET |
| Preceding reported trade resumption | 11:58:53 ET |

The critical quote boundary was:

| Saved status | Local response received (ET) | Provider quote time (ET) | Provider last-trade time (ET) |
| --- | --- | --- | --- |
| Last `Normal` before this halt | 11:59:16.383934 | 11:59:18.288 | 11:59:17.640 |
| First `Halted` in this transition | **11:59:18.279250** | **11:59:20.067** | 11:59:17.640 |

The first halted response's provider quote time is one millisecond after the
Nasdaq halt time, while the local response receipt is 1.787750 seconds before
that provider quote time. Quote time is not an explicit security-status
transition timestamp; its agreement with Nasdaq is corroborating evidence.

All 11 `Normal` rows in the extract have provider quote times later than local
receipt, by 0.762740 to 2.189382 seconds. The preceding resumption has the same
ordering: a `Normal` response received locally at 11:58:52.398100 contains a
provider trade timestamp of 11:58:53.480, after the reported 11:58:53 resumption.
This pattern extends beyond a halt-specific status interpretation.

Some equal-price observations genuinely preceded the reported halt status:
the `Normal` observations at approximately 11:59:04 and 11:59:16 both have a
$2.70 last price, while cumulative volume rises from 661,968 to 664,432
(2,464 shares) and provider last-trade timestamps advance. A price plateau
alone does not establish that a symbol is halted.

Source review confirmed that `quote.securityStatus`, `quote.quoteTime` and
`quote.tradeTime` are preserved in the corresponding stored fields. The chart
uses local acquisition completion for quote availability. In this extract,
response receipt and chart availability differ by approximately 0-2 ms, which
does not explain the seconds-scale discrepancy. No inspected code path applies
a later status to an earlier quote point or rounds the Nasdaq halt start to a
candle boundary.

## Independent clock measurement and correction

Before correction, `w32tm /query /status /verbose` reported that Windows Time
was not started (`0x80070426`). That establishes its state at the diagnostic
time, not whether it was stopped during the market session.

The operator then ran:

```cmd
w32tm /stripchart /computer:time.windows.com /dataonly /samples:5
```

The five samples, displayed at 20:48:21-20:48:30 on November 27, were
`+2.5284391`, `+2.5297239`, `+2.5287540`, `+2.5223298`, and `+2.5299886` seconds.
Mean: **+2.52784708 seconds**. A positive stripchart value means the remote
clock is ahead of MasterBot. This independently established a current local
clock lag with the direction and sufficient magnitude to explain the earlier
ordering, without establishing the exact historical offset.

The operator started the existing service with `net start w32time`. The first
`w32tm /resync` returned no time data; a second succeeded. The exact cause of
the initial failure was not determined. The subsequent status showed:

| Field | Observed result |
| --- | --- |
| Time source | `1.pool.ntp.org` |
| Last successful sync | 11/27/2026 20:52:43, as displayed by Windows |
| Stratum | 3 |
| Last sync error | 0 |
| Phase offset | -0.0011406 seconds |
| Reported poll interval | 1024 seconds (17 minutes 4 seconds) |

A separate five-sample check against `time.windows.com`, displayed at
20:53:10-20:53:18, returned `-0.0010907`, `-0.0018131`, `-0.0015232`,
`-0.0009788`, and `-0.0003702` seconds. Mean: **-0.0011552 seconds**, or
MasterBot about **1.16 ms ahead**. The observed range was 0.37-1.81 ms ahead.
This verifies the immediate correction; it does not guarantee long-term
millisecond accuracy.

## Persistent startup setting verified

The initial service configuration was `START_TYPE: 3 DEMAND_START`. The
trigger query showed two START actions (domain-joined and a custom system-state
event) and no STOP action. The custom trigger was not decoded or changed.

The operator applied:

```cmd
sc config w32time start= auto
```

The command returned `ChangeServiceConfig SUCCESS`. Follow-up queries verified:

```text
START_TYPE         : 2   AUTO_START
STATE              : 4   RUNNING
WIN32_EXIT_CODE     : 0
SERVICE_EXIT_CODE   : 0
```

The existing time-source configuration and start triggers were preserved.
No trigger deletion, NTP peer replacement, service restart or forced reboot
was required for the startup-setting change.

## Remaining distinctions and project continuation

- **Current host-clock correction:** completed and measured; automatic startup
  configured and current running state verified.
- **Historical chart anomaly:** strongly explained by local/source clock
  misalignment plus some normal equal-price trading. Retain original evidence;
  do not claim a precisely measured historical correction or an early exchange
  halt from the locally timestamped `Halted` response alone.
- **Nasdaq discovery latency:** the saved first observation minus reported
  start is 104.772426 seconds on the recorded clocks. This is separate from
  the few-second clock mismatch. The collector alternates CURRENT/HISTORICAL
  requests; the exact intervening feed content was not examined in this
  investigation. No claim is made that this discovery delay was fixed.
- **Transport/retry metrics:** remain deferred as previously agreed. A constant
  local-clock offset does not by itself lengthen a duration measured between
  two readings of that same clock.
- **Development:** the clock investigation introduces no further live-session
  gate before indicator-framework work.

## References

- [Microsoft: interpreting stripchart offsets](https://techcommunity.microsoft.com/blog/networkingblog/how-not-to-test-the-windows-time-service/411592)
- [Microsoft: Windows Time commands and settings](https://learn.microsoft.com/en-us/windows-server/networking/windows-time-service/windows-time-service-tools-and-settings)
- [Microsoft: sc.exe config and automatic startup](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/sc-config)
- [Microsoft: Windows Time workgroup trigger behavior](https://learn.microsoft.com/en-us/troubleshoot/windows-client/active-directory/w32time-not-start-on-workgroup)
