# Daily shared membership operating checkpoint (2026-11-11)

This checkpoint preserves the exact `daily_flow_membership_20261110.zip` delivered candidate. Its original package is intended to be extracted unchanged under `experiments/daily_flow_membership_20261110/`. The original archive is retained by the operator; it is **not** a production deployment or an automatic merge into `main`.

## Archive identity

- Original ZIP: `daily_flow_membership_20261110.zip`
- ZIP SHA-256: `a96af035abcb37db476b92d12b4419500c4fc6c0fa2c6dd2fff92779cdac3334`
- Original package entries: 158
- Source inventory: `daily_flow_membership_20261110/SOURCE_INVENTORY.json`; 157 hashed entries plus inventory itself, all verified against the ZIP.
- Includes code, tests, CMD entry points, docs, and synthetic validation logs; no credential/token databases or actual market journals.

## Acceptance evidence and limitations

- Operator-reported Windows offline verification: 304 core tests PASS, 41 read-only Overlay tests PASS (Plotly 6.5.2).
- November 10, 2026: Uni 780/780; Focus 1560/1560; Hot 9341/9360 (19 missing); journal audit FAIL solely for missing configured Hot slots; two immediate replays PASS with identical SHA-256 `a659782383eb18863da5da87b05872dc4e48943f47cebd26c2a72ab06c5b0ad3`.
- November 10: 87 Nasdaq-driven promotion revisions for 21 symbols; 87 unique (symbol, halted_at) identities, matching saved LUDP records; no promotion before eligibility cutoff.
- November 11: Uni 780/780; Focus 1560/1560; Hot 9351/9360 (9 missing); all three workers exited code 0 with `schedule_complete`; post-close next-session r0 published. Full journal audit and manual live-control acceptance pending.
- Known issues: occasional Hot quote-batch latency beyond 2-second cadence; intermittent Nasdaq feed failures lacking durable exception history; foreground daily status viewer may persist after FINISHED; manual promotion/demotion not yet live-accepted.

## Release procedure

1. Preserve the ZIP payload **unchanged** and verify its SHA-256 and `SOURCE_INVENTORY.json` before publishing.
2. Preserve evidence of the published Git tree and check each hashed file against the source inventory.
3. Integrate the candidate into the regular repository source layout and run Windows/native regression plus live manual-control validation.
4. Obtain explicit operator approval for any `main` merge or production activation.

Do not place secrets, encrypted configuration, OAuth artifacts, local live journals, or provider raw capture directories into Git.
