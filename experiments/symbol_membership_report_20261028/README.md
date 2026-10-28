# Membership changes and recorded reasons — October 28

Extract the new `membership_report_20261028` folder under
`C:\Users\danla\Documents\github\mb_market_data\output`. The original Monday
packet is retained. This tool needs no installation, API or password and can run
while production polling continues. It reads saved membership and selection
evidence without changing source, journals or pollers.

```cmd
call "C:\Users\danla\Documents\github\mb_market_data\output\membership_report_20261028\report_membership.cmd" "AZTR CLRO LUCD JAGX NN SAIQ" --date 2026-10-26 --end-time 16:00
```

Each symbol has timestamped joins, departures and reentries with recorded
reasons. Unchanged membership is coalesced. When Uni was never joined, only
`UNI: never a member | <reason>` is shown. Focus/Hot are suppressed by the
validated nested hierarchy. Hot is also omitted when Focus was never joined.
Missing membership history remains NULL rather than an exclusion.

The report uses matching, published-hash-bound decision ledgers for screening
reasons. Original source dates remain visible for a carry-forward roster.
An unavailable or changed ledger yields `reason unavailable`; the tool never
assigns an exclusion merely from a symbol's price, today's inputs, or another
symbol's diagnosis. The actual AZTR exclusion reason has not been established
in the development workspace and must come from the saved Windows evidence.

For ledgers archived elsewhere, append `--evidence-root "C:\path\to\archive"`.
This option is repeatable. The default search covers saved daily-universe
ledgers and the sibling `schwab_watchlists\output\ov_focus_production` folder.

For an optional offline check using synthetic journals and the currently
installed native store code:

```cmd
call "C:\Users\danla\Documents\github\mb_market_data\output\membership_report_20261028\verify_membership.cmd"
```

It does not read the production journals. See `report_details.md` for full
cutoff, missing-data, legacy-schema, JSON and evidence-binding semantics.
