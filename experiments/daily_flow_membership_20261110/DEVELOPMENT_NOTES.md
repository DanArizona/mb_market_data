# Development and acceptance scope

Base: uploaded daily_flow_three_20261110.zip, previously accepted on Windows
with 246 offline tests and an operator-observed desktop alarm demonstration.

Local development runs Linux/Python 3.13.5, exchange_calendars 4.13.1, Plotly 6.5.2.
mb_tools is unavailable here; the existing non-Windows test-only import substitute
is disclosed. Windows verification refuses that substitute. All broker/identity
responses and halt episodes in tests are synthetic; no provider calls were made.

Core verification: baseline 246 plus 47 membership/source/controller tests and
11 live-reader/process-connection tests. Separate chart suite: 41 tests reusing
the existing figure. Local JavaScript is additionally checked with Node syntax
validation; Node is not a runtime or Windows test prerequisite.

The actual collector proof uses three real child processes and a real schema-v2
journal, with a parent-controlled synthetic clock and responses. INTC demotion,
re-promotion, and a synthetic Nasdaq-source promotion are committed while those
same processes remain active. Native audit and repeated event-order replay must
match. The actual full daily controller with live RSS and broker traffic has not
been run in this environment. Windows acceptance of the new code is pending.

This is one separate development candidate, not an in-place production update.
Source publication to GitHub is not claimed by this package. Do not confuse the
retained baseline provenance reports with this candidate's local validation.
