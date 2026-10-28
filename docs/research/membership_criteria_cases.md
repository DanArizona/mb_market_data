# Membership criteria investigation register

Status updated October 28, 2026. This is a research backlog. Entries preserve observed membership and questions for later investigation; no screening rule or membership change is adopted by adding a case.

## Earlier cases retained

| Session date | Symbol | Prior finding | Investigation status |
| --- | --- | --- | --- |
| 2026-10-21 | POM | Reported volume accumulated during 09:00–09:30 ET, outside the Focus overnight-volume window ending before 09:00. | Retained for review of late premarket coverage and the measurement window. No criteria adjustment adopted. |
| 2026-10-21 | SDEV | Calculated market cap was $42,010,812.71, above the $40 million Uni ceiling, preventing Focus eligibility. | Retained for review of the hard market-cap boundary. No threshold adjustment adopted. |

These findings come from the earlier case review recalled and confirmed by the operator in the October 26 chat. Their underlying historical decision files have not been reexamined for this register.

## October 26 cases — exclusion reasons now reported; criteria review pending

All six were explicitly added by the operator on October 26. The saved production journal reports unchanged membership from 09:30 through the requested 16:00 ET cutoff. Membership before its first 09:30 revision is unknown.

| Session date | Symbol | Uni | Focus | Hot | Status / investigation question |
| --- | --- | --- | --- | --- | --- |
| 2026-10-26 | AZTR | No | No | No | Reported reasons: regular trade not in session; volume below minimum; close below minimum. Captured close 0.0715 and volume 0, input session October 22. Criteria review remains pending. |
| 2026-10-26 | CLRO | Yes | Yes | No | Included Uni comparison case: captured market cap 5,136,791.04, close 1.92 and volume 92,419, input session October 22. Focus joined at 09:30 r1; matching Focus reason ledger unavailable in the report. |
| 2026-10-26 | LUCD | No | No | No | Reported reason: market cap above maximum, captured 62,573,898.8210, input session October 22. Criteria review remains pending. |
| 2026-10-26 | JAGX | No | No | No | Reported reason: market cap below minimum, captured 1,118,301.00, input session October 22. Criteria review remains pending. |
| 2026-10-26 | NN | No | No | No | Reported reason: market cap above maximum, captured 1,553,521,451.22, input session October 22. Criteria review remains pending. |
| 2026-10-26 | SAIQ | No | No | No | Reported reason: market cap above maximum, captured 102,366,802.56, input session October 22. Criteria review remains pending. |

### Evidence and interpretation

- Source: [accepted Windows membership report](../validation/symbol_membership_report_20261026_windows_acceptance.json), from the complete operator CMD transcript and actual prompt return. The original Windows journal has not been independently read in the development workspace.
- Journal: `C:\Users\danla\Documents\github\mb_market_data\output\quote_observation_journal_v2_api_opening_2026-10-26\2026-10-26.sqlite3`.
- Requested symbols: `AZTR CLRO LUCD JAGX NN SAIQ`; date `2026-10-26`; end time `16:00 ET`.
- These are known membership results, not NULL/no-data results. CLRO is an included case, not a demonstrated missed selection. The October 28 enhanced report supplies the other five symbols' recorded Uni reasons from hash-matching saved ledgers, preserving their October 22 input provenance. See the [accepted Windows result](../validation/symbol_membership_changes_20261028_windows_acceptance.json). Original Windows journal and ledger files were not independently uploaded or read here.
- Production Hot was disabled for this session. Absence from Hot is therefore expected and does not itself demonstrate a criteria failure or failed promotion.
- Membership does not by itself establish quote validity, event coverage, or that a different selection would have been useful.

### Deferred evidence review

1. Original October 22 selector provenance is now exposed by the operator's enhanced report for the approved October 26 carry-forward. The fresh October 26 post-close build targets October 27 and is not evidence of October 26 opening eligibility. Any deeper input audit still needs the original evidence files.
2. The five reported exclusion reasons and captured values are retained in the October 28 acceptance record. Further review can examine their source timestamps and any missing or invalid input. Do not assign the POM or SDEV explanation to a new case without evidence.
3. For CLRO, retain its Uni inclusion and locate the hash-matching Focus ledger if the OV ranking/window reason is needed. Its missing Focus/Hot explanation remains explicit.
4. Compare selection evidence with the day's relevant activity, then record whether a possible improvement concerns timing, eligibility boundaries, data quality, or another cause. Any specific criteria proposal remains a separate decision.

This backlog does not add a prerequisite ahead of the current automatic Nasdaq/Hot, manual membership, and agreed read-only Overlay priorities.
