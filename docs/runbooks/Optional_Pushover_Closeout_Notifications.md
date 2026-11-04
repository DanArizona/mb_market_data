# Optional Pushover mobile notification: inactive integration contract

**Status: STUB ONLY.** No Pushover account is configured. This experiment
cannot send a push, does not read credentials, does not access the network,
and never reports `DELIVERED`.

The extension point is `src/mb_market_data/closeout_notifications.py`:
- `CloseoutNotice`: typed event name, source session, optional target,
  title, message and urgency. Tokens/passwords are not fields.
- `CloseoutEvent.PREPARATION_DUE`, `DEADLINE_MISSED` and
  `PREPARATION_READY` are reserved for a future alert lifecycle.
- `CloseoutNotificationChannel`: `dispatch(notice) -> DeliveryResult`.
- `PushoverStub`: returns `DISABLED` normally, `NOT_IMPLEMENTED`
  even when explicitly instantiated with `enabled=True`.
  `delivered` remains false. The desktop reminder's DUE path calls the
  stub but **never sends a push or requests a password**.

A past `ToS_scanner` integration used the encrypted Pushover configuration
file `secure/pushover.ecfg`, `mb_tools.secure_config.load_ecfg`, and
credential keys `MB_PUSHOVER_APP_TOKEN` and `MB_PUSHOVER_USER_KEY`.
Older experiments used environment keys `PUSHOVER_APP_TOKEN` and
`PUSHOVER_USER_KEY`; those are **not used by the new stub**.

After the account is reinstated, implementing delivery requires:

1. Explicit user opt-in and authorization for live provider/network calls.
2. A separate secure credential-loading design. Do not embed tokens, user
   keys or decryption passwords in the repository, Task Scheduler command
   arguments, log files, receipts or environment setup scripts.
3. Define how the unattended *reminder* can securely retrieve credentials.
   The existing scanner's password-dialog decryption pattern is not
   automatically suitable for a scheduled silent task. Do not make the
   user type a second credential during daily closeout.
4. HTTPS request to Pushover's supported Messages API with `token`,
   `user`, `title`, `message` and `priority`. A HTTP 200 response
   and body `status=1` mean accepted/queued, **not confirmed read**.
   Failure/timeout must be visible and never suppress the desktop reminder.
5. Decide notification policy and duplicate throttling. Proposed:
   ordinary due (priority 0), one missed-deadline escalation (priority 1),
   optional recovery resolved (priority 0). The past scanner used distinct
   sounds; do not assume sound preferences transfer without explicit review.
6. Fake-server / network-denied tests, credential redaction tests and a
   user-approved one-message live test before production activation.

Official specification: https://pushover.net/api

This stub must remain a no-op in production until account setup, explicit
approval, security review and live transport qualification are complete.
