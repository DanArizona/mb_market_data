# Changes from the accepted daily_flow_three_20261110 candidate

Added daily_membership.py, daily_nasdaq_source.py, daily_live_overlay.py, two CLI
probes and membership.cmd/overlay.cmd. Added focused shared-writer and live-reader
tests, an actual three-child dynamic-membership proof, and optional chart checks.

The daily runtime optionally owns membership/source workers after opening
publication, passes credentials only to the membership worker, and reports their
states. The health inspector includes those optional services. The native quote
acquisition/store implementation, 09:25 ranking policy, interleaved slot cadence,
attended authorization, and post-close builder have not been replaced.

The existing Observation Overlay accepts cache=None for explicitly quote-only
live views; its original chart builder is reused. The standalone adapter is
read-only, not a complete replacement for the established dashboard.

The older Coordinator request-ID UI and standalone Hot runtime are not launched.
The new small adapter commits command/state and nested native membership together.
Ordinary symbol-level demotion and Uni-only addition are explicit behaviors.
There is no persistent Suppress Automatic, full manual GUI, or live order entry.

New flags are opt-in. Existing fallback code/data and prepared r0 are unchanged.
