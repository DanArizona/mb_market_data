# Offline polling history and recovery review

This bundle runs from its own extracted folder. It does not install source,
contact Schwab or Nasdaq, prompt for credentials, or open production journals.
All generated data is synthetic. Production Uni/Focus can continue normally;
this proof still uses local CPU, memory and disk.

1. Run `15_verify_history.cmd`: 63 focused tests, then a three-minute logical
   session with its final minute scheduled at actual elapsed speed. It includes
   cold startup and four abrupt process exit/recovery drills.
2. After PASS, run `16_run_full_history.cmd`: 389 logical minutes of native
   collector prefill followed by the final minute at actual elapsed speed.
   Membership starts at 554 Uni / 40 Focus / 40 Hot. This represents 780 Uni,
   1,560 Focus and 11,700 Hot session acquisitions, roughly 962,520 observations.
   The tail exercises outside-Uni admission, source withdrawal, a temporary cap,
   expiry reentry, and delayed demotion. It uses review policy settings only.
3. `17_stop_history.cmd` requests a stop for this package's active proof only.
   Ctrl+C also stops it. Keep the production health monitor visible.
4. `18_assess_saved_combined.cmd "FULL_PATH_TO_OLD_RUN"` performs a read-only
   audit and two replays of a saved legacy combined trial. It writes a separate
   uniquely named assessment under this package's `runs/assessments` directory.
   Original reports and journals are never overwritten.

Every run creates a fresh folder. Commands use their own directory and work
from any current CMD directory. No package installation is needed. Use the
existing Python environment with the project's dependencies.

The full proof requires at least 6 GiB free space. Prefill, cold reconstruction,
repeated recovery and full replay can take several minutes. Progress is printed
during prefill and startup/recovery waits. Startup is serialized and the clock
is held until all four processes are ready. Startup cost is reported separately
from warm polling cost. The original 750 ms dispatch deadline remains in force;
slow warm work remains a visible failure rather than causing catch-up requests.

Drills use the same accumulated Hot history and a separate synthetic minute
after the session. Exits after response assembly and journal commit must recover
without another quote request. Exits before durable response assembly produce
two explicit gaps; they are deliberate, separately reported recovery outcomes.
The regular-session coverage check excludes those drill slots.

Reports separate collection/integrity, transport and individual availability.
Explicit `invalid` observations produce `PASS_WITH_WARNINGS` if all independent
checks pass. `request_error`, missing response rows, unknown statuses, or any
integrity/coverage failure still produce FAILED. An invalid status does not
explain why the provider rejected a symbol or establish its tradability.

This is a history/growth and recovery test, not six-and-a-half hours of wall-clock
operation or a full-session Schwab capacity claim. Coordinator request history
is exercised during the final minute; it is not backfilled for the whole day.
Production Hot activation, live Nasdaq transport, UI integration and merging
into main remain later steps.
