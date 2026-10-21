# Nasdaq RSS pacing qualification, October 20

Live source policy now waits at least 60 seconds after completion of each feed
attempt (including unavailable responses), maximum 15 attempts in 15 minutes.
Nasdaq Trade Halt RSS refresh guidance was checked on October 20:
https://www.nasdaqtrader.com/Trader.aspx?id=TradeHaltRSS
It describes a once-per-minute update and query frequency. The previous live
two-second RSS configuration is superseded, with its failed runs preserved.
This does not prove why a security layer intervened or that access is fixed.

Synthetic feeds keep their offline two-second interval. Hot quote polling
continues every two seconds after membership is effective. Source publication
and minute-spaced acquisition can delay halt discovery; this is not a two-second
discovery service or a live latency bound. No production source was changed.

Source commit: 34930a035aa042235b81c7501a99f319c7a69ab8
Bundle: nasdaq_shadow_20261020_155417_f87c4b.zip
SHA-256: 141b63ffee543088dc52272c255a9b9aa7220bea576a9ebf4b8da96d05e908c7

52 affected tests passed locally in 4.148s. An extracted package under a path
containing spaces passed all 52 checks in 3.590s and the actual-elapsed synthetic
HTML-recovery proof: PASS_WITH_WARNINGS (SYNTHETIC_TRANSITION), 3/3 Hot, three
empty slots, zero misses, one identity plus three Hot synthetic quote calls.
Feed RECOVERED_WITH_GAPS (four valid, one unavailable); discovery continuity
NOT_ESTABLISHED. Audit and repeated replay passed. The attached log/report are
generic synthetic proof only; no private operator data is included here.

Native Windows verification of this exact new package and a separately invoked
minute-spaced live observation are pending. No live request, credential access
or production polling/journal modification occurred in this qualification.
