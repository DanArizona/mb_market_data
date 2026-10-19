# Guarded incremental Hot acquisition review

The previous authenticated 40-symbol trial missed 42/150 two-second slots. Full-history verification grew while request latency remained much smaller. This opt-in successor removes historical revalidation from normal per-slot work and retains the 750 ms dispatch deadline at both gates.

Fresh experimental journals only. `IncrementalSamplingPoller` requires two explicit opening markers. Its SQLite guards forbid changes/deletion/replacement of committed rows and prevent late extension of already complete observation/member sets. The process pins storage identity and schema epochs. Each normal read validates new hash-chain events, current slot-time membership, new journal outcomes and the current completed acquisition. Other channels may append independently. A compact in-memory checkpoint retains event headers, final outcomes and the pending response; old response bodies remain on disk.

Startup, lost-checkpoint recovery, explicit snapshot and audit still reconstruct/check the full history. The independent full reader remains available and the default trial mode is unchanged. Guards protect against ordinary SQL changes and accidental replacement; they are not an adversarial security boundary against a database administrator. Full startup scans grow with history and are outside polling capacity measurements.

## Run on MasterBot

Extract the unique ZIP into `C:\Users\danla\Documents\github\mb_market_data\output`, retaining its named top-level folder. Use sea-green. Absolute `call` paths work from any directory, including one containing spaces.

1. Run `05_verify_hot_incremental.cmd`. Focused tests run first, then 300 logical prefill acquisitions / 12,000 rich synthetic observations. Progress prints every 50 acquisitions. The following eight slots use actual elapsed work/waits at 2 seconds, with 250 ms injected synthetic latency. Slow work can miss the unchanged deadline. This takes roughly 18 seconds after prefill; the final full audit/replay adds time.
2. After PASS, run `06_export_hot_trial_payloads.cmd`. It reads only the retained isolated 40-symbol trial sidecars, checks every original event-chain link, and exports successful decoded quote samples from the first/middle/last response in each phase. It does not read credential/config/token files, transport headers, or provider error bodies. Source databases are opened read-only.
3. Attach the printed export ZIP plus verification summary. Replay captured payloads before scheduling another authenticated trial.

No API/password or new Schwab acquisition is launched by these two commands. The general probe supports explicit `--verification-mode guarded_incremental` for a later reviewed bounded trial; no live launcher is provided yet. Production polling, membership and cadence remain independent.

## Validation limits

Rich synthetic fixtures exercise local normalization/storage/scheduling with growing history. They do not establish Schwab capacity. Captured-payload replay, native Windows acceptance, the repeated authenticated 40-symbol trial, combined Uni/Focus/Hot load, trading-hours capacity and production adoption remain pending. Gate diagnostics separate state preparation, dispatch spooling, before-dispatch/before-API lateness and pre-slot recovery. All received-row quote status is reported separately from planned-slot coverage.

### Local review anomaly

The first extracted run passed all 83 focused tests and all 300 prefill acquisitions, then stopped on the storage/schema guard during its first elapsed response. The original generic error did not identify which guard component differed. A same-process reproduction and an extracted rerun both passed. The cause remains unconfirmed; the failure is retained in validation. Diagnostics now report file identities, schema epochs and guard-identity agreement; a failed elapsed proof also preserves failure.json and its databases. The guards remain strict. Native review and captured-payload replay come before any authenticated successor trial.

A later asynchronous extracted run also stopped on a ledger-head mismatch. Synchronous proofs in /tmp and the workspace, a longer 30-slot proof, and an instrumented asynchronous reproduction passed. These passes do not establish the cause of either earlier stop. Both failures remain in validation; an authenticated successor remains pending.

The latest diagnostic stop confirms that both OS file identities changed while schema epochs and guard-identity payloads still matched. The origin of that identity change remains unconfirmed. This is a review candidate with an unresolved storage-consistency investigation, not acceptance for another API run.
