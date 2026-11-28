# MasterBot timing runbook

## Purpose and current checkpoint

Use this guide to check MasterBot's clock, correct an observed offset, or
diagnose Windows Time startup behavior. It is an operator reference, not a
list of commands that must all be run each day.

Run commands on **MasterBot in Command Prompt opened as Administrator**.
Any working directory is suitable; no conda environment is required.

The November 27, 2026 checkpoint is already complete:

- Windows Time startup: `2 AUTO_START`.
- Current service state at verification: `4 RUNNING`, with zero exit codes.
- Working synchronization source: `1.pool.ntp.org`.
- Latest independent measurement: approximately 0.4-1.8 milliseconds ahead of
  Microsoft's time server, following correction of a roughly 2.53-second lag.

These are recorded results, not a guarantee of future accuracy. Nothing needs
to be rerun solely because this guide has been created.

## 1. Which descriptions refer to the same action?

The original eight bullets describe **six distinct actions**. These three
descriptions all mean the same measurement:

- `w32tm /stripchart /computer:time.windows.com /dataonly /samples:5`
- "Measure five samples against Microsoft's time server."
- "Direct NTP probe to measure MasterBot's offset against Microsoft's time server."

| Action | Command | Effect / question answered |
| --- | --- | --- |
| Measure clock offset | `w32tm /stripchart /computer:time.windows.com /dataonly /samples:5` | Read-only comparison: how far ahead or behind is MasterBot now? |
| Inspect synchronization status | `w32tm /query /status /verbose` | Read-only: what source is Windows Time using, and when did synchronization last succeed? |
| Start Windows Time | `net start w32time` | Starts the service now. Automatic startup is a separate setting. |
| Request synchronization | `w32tm /resync` | Requests a clock correction using the configured synchronization source. |
| Inspect startup configuration | `sc qc w32time` | Read-only: is startup Automatic, Manual/Demand, or Disabled? |
| Inspect service triggers | `sc qtriggerinfo w32time` | Read-only: which events are configured to start or stop the service? |

## 2. Normal check: measure, then inspect status

Use this when checking after a restart, after timing-related changes, or when
market-data timestamps appear inconsistent. It is not a required daily repair
procedure.

### A. Measure the clock offset

```cmd
w32tm /stripchart /computer:time.windows.com /dataonly /samples:5
```

This collects five offset samples and exits. It makes diagnostic NTP requests;
it does not set the clock or change the configured time source. It can measure
the offset even when the background Windows Time service is stopped.

Read the sign and units:

| Example | Meaning |
| --- | --- |
| `+02.528s` | Microsoft is ahead; MasterBot is about 2.528 seconds slow. |
| `-00.001s` | Microsoft is behind; MasterBot is about 1 millisecond fast. |

Stable readings near zero, like the recent millisecond-level measurements,
indicate good current agreement. A repeated multi-second offset warrants
correction. Five samples are a short diagnostic check, not a long-term accuracy
certification or a newly adopted project acceptance threshold.

### B. Inspect Windows Time's synchronization status

```cmd
w32tm /query /status /verbose
```

The most useful fields are:

- `Source`: the source used for synchronization.
- `Last Successful Sync Time`: the last successful synchronization.
- `Last Sync Error`: zero indicates the last synchronization succeeded.
- `Phase Offset`: the service's reported clock offset relative to its source.

If the offset measurement is satisfactory and status shows an appropriate
source with a recent successful synchronization, the check is complete.

**Reference server versus synchronization source:** `time.windows.com` in the
stripchart command is the comparison target. MasterBot's working configured
source is `1.pool.ntp.org`. Those can be different; measuring against Microsoft
does not switch Windows Time to Microsoft.

If status returns `The service has not been started (0x80070426)`, Windows Time
is stopped at that moment. The stripchart measurement remains useful. A stopped
service alone does not establish its historical state or prove a clock error.

## 3. Correction: use only when needed

For an investigation, take the offset measurement before correction so the
original condition is recorded.

### A. Start the service if it is stopped

```cmd
net start w32time
```

If it is already running, this step is unnecessary. Starting the service may
itself begin synchronization; it does not set automatic startup.

### B. Request synchronization

```cmd
w32tm /resync
```

This uses the existing configured source. If a newly started service reports
"no time data was available," allow a few seconds for discovery and retry once.
If that error persists, retain the output for source/connectivity diagnosis.
A successful command is followed by measurement rather than further retries.

### C. Verify the result with the same two checks

```cmd
w32tm /stripchart /computer:time.windows.com /dataonly /samples:5
```

```cmd
w32tm /query /status /verbose
```

These are the same actions from section 2, repeated for a **before-and-after
comparison**. The first measurement establishes the problem; the second checks
the effect of the correction. Windows can correct gradually, so immediate
measurements may still be changing.

## 4. Startup configuration: separate from clock correction

Use these checks when startup behavior is in question, not every time you
measure the clock.

### A. Inspect the configured startup type

```cmd
sc qc w32time
```

MasterBot's accepted setting is:

```text
START_TYPE         : 2   AUTO_START
```

The configuration-changing command used to establish that setting was:

```cmd
sc config w32time start= auto
```

**This change has already been completed on MasterBot.** It configures startup
at Windows boot. The space after `start=` is required. Run it again only if
intentionally restoring this setting after a configuration change; verify
with `sc qc w32time`.

### B. Inspect triggers if startup or stopping behavior is unexpected

```cmd
sc qtriggerinfo w32time
```

This lists service start/stop conditions. It does not report the NTP polling
interval. MasterBot's inspected configuration had two START triggers and no
STOP trigger. Both start triggers were preserved. Inspect actual output before
considering any trigger changes.

### C. Optional: check whether the service is running right now

```cmd
sc query w32time
```

Expected while running:

```text
STATE              : 4   RUNNING
```

`sc qc` answers **how startup is configured**. `sc query` answers **whether the
service is running now**. Neither measures clock accuracy.

## 5. What to send back if help is needed

- For a clock-offset concern: the five stripchart samples and synchronization
  status, or the exact error.
- After correction: the synchronization result and the repeated offset/status
  checks.
- For a startup problem: `sc qc`, `sc qtriggerinfo`, and current `sc query` output.

A stripchart timeout means that comparison did not yield a measurement; it does
not establish whether the clock is right or wrong. Keep the working source
configuration while diagnosing the actual failure.

## 6. Limits relevant to mb_package

Current clock measurements do not establish the exact offset during an earlier
market session. Preserve recorded journal timestamps rather than adjusting old
observations using a later clock measurement. Nasdaq report-discovery latency
is a separate measurement from the host-clock offset.

The detailed incident evidence is in
[Halt Timing Investigation, November 27](https://github.com/DanArizona/mb_market_data/blob/d2b31cb062b6b49d3b909f5758b9b8fd3109e83d/docs/runbooks/Halt_Timing_Investigation_2026-11-27.md).

## References

- [Microsoft: Windows Time commands and settings](https://learn.microsoft.com/en-us/windows-server/networking/windows-time-service/windows-time-service-tools-and-settings)
- [Microsoft: stripchart offset sign and measurement limitations](https://techcommunity.microsoft.com/blog/networkingblog/how-not-to-test-the-windows-time-service/411592)
- [Microsoft: sc.exe config](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/sc-config)
- [Microsoft: service trigger events](https://learn.microsoft.com/en-us/windows/win32/services/service-trigger-events)
