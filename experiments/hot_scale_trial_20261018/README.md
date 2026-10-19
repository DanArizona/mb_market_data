# Longer, larger-roster Hot trial

This follow-up starts from the accepted native integration/provider pilot,
commit `28523939e5d05ba684bf899910ce9cb5dd7c3a43`. Production main stays at
`a082f544112643b4e5f459d15c67f60d0a1ef83d`. The original accepted ZIP is preserved.

The first authenticated pilot completed 45 acquisitions / 135 quotes on
MasterBot. It also exposed local work beyond the provider's ~0.23s request
latency. An offline 40-symbol profile found repeated normalization of every
historical response and repeated journal connections/membership queries. Around
acquisition 30 the original complete local cycle was ~440ms. Reusing only the
derived normalized tuples and sharing journal connections/validated revision
lookups reduced that local synthetic cycle to ~121ms. These figures are local
profiles, not Schwab capacity measurements.

## Integrity and scope

Every read still validates the full evidence hash chain and current journal
headers, observation values, channel outcome sets and slot-time membership.
The cache stores derived normalization only, keyed by acquisition identity and
an assembled-event hash verified in that read. Changed payload hashes cause a
fresh normalization; changed live journal rows are compared and refused even
with a warm cache. Connections close after each read, including exceptions.
The per-instance cache is bounded to 1024 acquisitions and discarded on restart.
A restart reconstructs from durable evidence. This remains a full-history scan;
its time still grows, and this increment makes no full-session capacity claim.

Existing acquisition tests remain standing. Six new checks cover warm-cache
journal tampering, coherently rehashed response changes, discarded/rebuilt
normalization, restart, connection reuse and explicit trial/growth bounds.
No daily production journal, process or environment installation is changed.
The legacy small-roster presets remain available in this branch; the current
source is a successor to the original byte-identical integration. Accepted
historical ZIPs and their original manifests remain unchanged.

## Run on MasterBot

Extract this uniquely named bundle under `mb_market_data\output`. In the
existing sea-green CMD environment, call the absolute path to
`03_verify_hot_scale.cmd` from any directory. It checks the payload, runs 60
focused tests, a short actual-clock synthetic check, then a logical-clock
40-symbol growth proof of 150 + 75 acquisitions and 9000 observations. The
logical proof jumps clock waits; it measures local processing costs without a
ten-minute wait or API requests. Progress is printed every 10 acquisitions.
Final audits/replays may take additional time. It labels the clock and quote
source explicitly and does not claim real-time provider capacity.

After PASS, call `04_run_schwab_hot_scale.cmd` separately. It prompts once for
the existing encrypted-config password. No subsequent command belongs in that
same pasted block. It runs about ten minutes plus setup and verification:

| Phase | Roster | Duration | Interval | Maximum quote calls | Requested rows |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 40 | 5 minutes | 2 seconds | 150 | 6000 |
| 2 | 40 | 5 minutes | 4 seconds | 75 | 3000 |

The fixed roster is the user's validated October 16 opening Focus set, used as
an isolated test input: NCPL TCRT WFF SNTI GPUS ESLA TWG FNGR BAOS BURU HAIN SOAR
EONR WSHP AMOD OMEX ATCH INV MRNO OLB ARAI DDC FFAI BEEM GNS TC JBDI FFR BGL NWGL
VNTG BENF SURG MOBX BEAT AZIO XHLD QNME MTEK PMI. This does not publish a new
production Focus or Hot roster. Current provider invalid/missing/error outcomes
are recorded explicitly; prior validity is not a promise of current responses.

All 40 fit one batch. The hard quote-client call budget is 225, with an explicit
3-second request timeout through the existing secure-client interface. Token
refresh uses the existing token database normally; SDK authentication calls are
outside the quote counter. OAuth prompting is refused. No secret, HTTP error
body or arbitrary SDK exception message is included in the reports. Ctrl+C
stops this trial and preserves evidence; allow a current request/database
operation to return. No catch-up request is sent for missed/unknown slots.

## Reports and next boundary

`runs/hot_scale_schwab/<unique-run>/summary.txt` and `manifest.json` give the
summary; each phase retains its own report, schema-v2 journal and acquisition
sidecar. Reports now add verification cost within each cycle, total verification
including recovery/final snapshot, normalization/cache statistics, and cycle
mean/p95/max for first/middle/last thirds of completed acquisitions. These thirds
are ordered by completed acquisitions, not equal elapsed-time buckets; missing
slots remain separately visible. Nested verification/collector/journal costs
must not be added to active-cycle wall time. Report code hashes and software
versions identify the run. Acquisition completion alone does not mean successful
quotes; statuses and complete configured coverage are separate checks.

Successful weekend/after-hours results measure transport and local work for
this roster/window. They do not establish market-hours freshness, live halt
latency, simultaneous Uni/Focus/Hot capacity, or a production Hot cadence.
Next review the growth/capacity results, then test combined channel traffic and
trading-hours behavior. Production catalog/broker admission, source
transport/authentication, genuine new live-halt proof and adoption remain open.


## MasterBot result — October 18 evening

The supplied console tail reports offline verification PASS (225 synthetic
acquisitions and 9000 quotes). The complete 60-test listing was not included.
The authenticated larger-roster trial **failed two-second coverage**:

| Interval | Acquisitions | Missed slots | Mean request | Mean complete cycle |
| --- | --- | --- | --- | --- |
| 2 seconds | 108/150 | 42 | 252 ms | 827 ms |
| 4 seconds | 75/75 | 0 | 281 ms | 711 ms |

All 7320 actually recorded observations are quotes, with no reported provider
error outcomes. The summary's `all requested rows are quotes=False` includes
missing planned acquisitions; it is not evidence that returned quote rows
were invalid. The process completed and returned to CMD, but this is not a
successful two-second capacity result. Four seconds covered this five-minute
phase only; no all-day capacity claim follows.

No misses were injected. The deliberately strict 750ms dispatch-lateness gate
skips a slot before its quote request if verification/persistence has already
consumed that budget. It is separate from the two-second period. The largest
completed 2s-phase cycle was 1.449s, so explaining these misses as cycles simply
exceeding two seconds would be incorrect. Full-history verification inside
and ahead of each request grows: first/middle/last-third cycle means were
505/823/1152ms at 2s and 454/734/943ms at 4s. The evidence strongly points to this
local work and the dispatch deadline; precise per-slot gate timings remain in
the unrequested host report/sidecar. The offline logical proof never claimed
wall-clock deadlines and used smaller synthetic payloads.

The next priority is to remove growing full-history work from normal per-slot
processing while preserving durable incremental integrity and full restart/
audit checks, then repeat this isolated measurement before combined-load work.
Add explicit timings for the lateness gates and distinguish received quote
success from planned coverage in summaries. This result is retained in
`validation/masterbot_scale_result_20261019.json`; implementation/tests and the
accepted test ZIP are unchanged. No rerun is needed for this documentation-only
record, and another provider run is not the next action.
