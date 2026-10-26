# Membership criteria investigation register

Status updated October 26, 2026. This is a research backlog. Entries preserve observed membership and questions for later investigation; no screening rule or membership change is adopted by adding a case.

## Earlier cases retained

| Session date | Symbol | Prior finding | Investigation status |
| --- | --- | --- | --- |
| 2026-10-21 | POM | Reported volume accumulated during 09:00–09:30 ET, outside the Focus overnight-volume window ending before 09:00. | Retained for review of late premarket coverage and the measurement window. No criteria adjustment adopted. |
| 2026-10-21 | SDEV | Calculated market cap was $42,010,812.71, above the $40 million Uni ceiling, preventing Focus eligibility. | Retained for review of the hard market-cap boundary. No threshold adjustment adopted. |

These findings come from the earlier case review recalled and confirmed by the operator in the October 26 chat. Their underlying historical decision files have not been reexamined for this register.

## October 26 cases — pending membership investigation

All six were explicitly added by the operator on October 26. The saved production journal reports unchanged membership from 09:30 through the requested 16:00 ET cutoff. Membership before its first 09:30 revision is unknown.

| Session date | Symbol | Uni | Focus | Hot | Status / investigation question |
| --- | --- | --- | --- | --- | --- |
| 2026-10-26 | AZTR | No | No | No | PENDING — trace the actual opening Uni decision and input provenance; evaluate the exclusion against the day's evidence. |
| 2026-10-26 | CLRO | Yes | Yes | No | PENDING — preserve as an included comparison case; review why it qualified and what useful activity the membership covered. |
| 2026-10-26 | LUCD | No | No | No | PENDING — trace the actual opening Uni decision and input provenance; evaluate the exclusion against the day's evidence. |
| 2026-10-26 | JAGX | No | No | No | PENDING — trace the actual opening Uni decision and input provenance; evaluate the exclusion against the day's evidence. |
| 2026-10-26 | NN | No | No | No | PENDING — trace the actual opening Uni decision and input provenance; evaluate the exclusion against the day's evidence. |
| 2026-10-26 | SAIQ | No | No | No | PENDING — trace the actual opening Uni decision and input provenance; evaluate the exclusion against the day's evidence. |

### Evidence and interpretation

- Source: [accepted Windows membership report](../validation/symbol_membership_report_20261026_windows_acceptance.json), from the complete operator CMD transcript and actual prompt return. The original Windows journal has not been independently read in the development workspace.
- Journal: `C:\Users\danla\Documents\github\mb_market_data\output\quote_observation_journal_v2_api_opening_2026-10-26\2026-10-26.sqlite3`.
- Requested symbols: `AZTR CLRO LUCD JAGX NN SAIQ`; date `2026-10-26`; end time `16:00 ET`.
- These are known membership results, not NULL/no-data results. CLRO is an included case, not a demonstrated missed selection. Exclusion reasons for the other five have not yet been established.
- Production Hot was disabled for this session. Absence from Hot is therefore expected and does not itself demonstrate a criteria failure or failed promotion.
- Membership does not by itself establish quote validity, event coverage, or that a different selection would have been useful.

### Deferred evidence review

1. Trace the October 26 approved carry-forward opening proposal to its original universe decision ledger and source-date inputs. The fresh October 26 post-close build targets October 27 and is not evidence of October 26 opening eligibility.
2. For each excluded symbol, retain the recorded rejection reason(s), the values and timestamps used, and any missing or invalid input. Do not assign the POM or SDEV explanation to a new case without evidence.
3. For CLRO, retain its Uni inclusion and Focus OV ranking/window evidence as a comparison.
4. Compare selection evidence with the day's relevant activity, then record whether a possible improvement concerns timing, eligibility boundaries, data quality, or another cause. Any specific criteria proposal remains a separate decision.

This backlog does not add a prerequisite ahead of the current automatic Nasdaq/Hot, manual membership, and agreed read-only Overlay priorities.
