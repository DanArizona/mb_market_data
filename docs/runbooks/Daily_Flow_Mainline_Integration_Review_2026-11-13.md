# Daily-flow mainline integration review — 2026-11-13

**Status: EXPERIMENTAL. Not merged, deployed, or an authorization to replace installed polling.**

## Provenance

- Base is \`mb_market_data main\`, commit \`a082f544112643b4e5f459d15c67f60d0a1ef83d\`.
- Source is \`experiment/daily-flow-membership-checkpoint-20261111\`, commit \`cfd22d771a75bcd55810eccf05536f4f1a396496\`.
- Accepted original ZIP SHA-256: \`a96af035abcb37db476b92d12b4419500c4fc6c0fa2c6dd2fff92779cdac3334\`.
- Exactly 117 source/probe/test files moved from \`experiments/daily_flow_membership_20261110/market_data/\` into the normal \`src/\`, \`probes/\`, \`tests/\` directories **without changing their file bytes**. The supplied \`INTEGRATION_SOURCE_INVENTORY.json\` checks their SHA-256 values.
- Against the prior main tree: 73 of those files were already identical, 33 are added, and 11 are updated. All other main paths are preserved.
- Root command launchers were adjusted to reference \`probes\\\` directly. Root offline verification scripts were adapted to the integrated directory structure.
- The original ZIP, checkpoint branch, development evidence, and fallback installation remain intact.

## Scope

Opt-in one-attended-unlock daily controller; 09:25 ET current-volume Focus selection; interleaved Uni/Focus/Hot collection; bounded atomic hierarchical membership with manual promotion and ordinary demotion; automatic Nasdaq LUDP/M feed requests; independent monitoring; read-only quote-only Observation Overlay; automatic post-close next-day r0.

Do not run the legacy standalone Hot coordinator against the same journal as this daily membership worker. The read-only Overlay does not fabricate candle data or replace historical replay. No live order execution or remote membership editor is added.

## Verification (isolated nonproduction)

- Original accepted ZIP: offline 304 core tests PASS, Overlay 41 tests PASS, no provider calls.
- Relocated source in Linux/Python 3.13.5: offline 304 core tests PASS with disclosed **non-Windows test-only authentication stub**; Plotly 6.5.2 Overlay 41 tests PASS; all 117 accepted source hashes verify. No provider calls, credentials, or production changes.
- On Windows, `core.autocrlf` may materialize CRLF working-copy lines while accepted Git source blobs remain LF. Both offline verifiers accept **only** the exact recorded SHA-256, or (on Windows) the same SHA-256 after CRLF-to-LF normalization. Other content changes still block verification; the accepted 117 files and their manifest remain unchanged.
- Windows live sessions Nov 11–13 demonstrated normal collector exit, journal integrity, identical pairs of deterministic replay, matched Nasdaq admission identities, and Nov 12 INTC/PLBL manual transitions. These runs used the separately installed accepted package, **not** this integrated checkout.

## Limitations and safety gates

- Native journal audits remained **FAIL** for Nov 11, 12, and 13: respectively 9, 8, and 6 Hot missed slots. Recorded slow acquisitions crossed every missing slot, but their underlying latency cause remains unresolved. Never convert audit FAIL to PASS.
- Provider-invalid responses: Nov 12 KAZR intermittent responses and CRMN exact-identity rejection during ticker transition; Nov 13 CLGN intermittently invalid (694 invalid Uni observations). The strict identity gate must remain.
- Successful Linux tests are **not** Windows release qualification. Run the Windows offline suites in an isolated checkout before review of any main merge.
- The \`pyproject.toml\` requirements and the existing \`mb_tools\` secure authentication dependency require Windows compatibility review before deployment.
- No production activation, credential access, journal edits, or main merge accompanies this branch.

## Offline Windows review (separate checkout only)

After checking out this experiment branch into a NEW folder, using the existing environment:

    conda activate closeout-qa
    cd /d "PATH_TO_SEPARATE_EXPERIMENT_CHECKOUT"
    call verify_offline.cmd

Overlay tests in another Anaconda CMD window with Plotly:

    conda activate dashing
    cd /d "PATH_TO_SEPARATE_EXPERIMENT_CHECKOUT"
    call verify_overlay.cmd

Do **not** invoke \`daily.cmd start --live\` as a checkout validation step. Do not change the installed \`dailyflow_membership_20261110\` or fallback environments. A merge to main or replacement of the installed system requires explicit user approval.
