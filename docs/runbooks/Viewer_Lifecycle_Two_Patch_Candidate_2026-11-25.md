# Viewer lifecycle fixes — isolated mainline candidate (2026-11-25)

**Scope:** Exactly two lifecycle corrections: (1) Overlay `Stop server` confirmation; (2) foreground daily CMD status viewer auto-return / Q. The candidate is based on `mb_market_data` main `de1e5544` and is NOT merged into main or installed in production. It deliberately excludes the unified Overlay branch's Real Time, experimental Membership, and OHLCV work.

## Included changes

- `src/mb_market_data/assets/overlay_stop_confirmation.js`: after the browser's confirmed Stop request, probe only the same-origin localhost listener; show a clear stopped screen after two failed probes, otherwise report shutdown not confirmed. This does not stop the polling controller.
- `probes/dashboard_quote_observation_journal.py`: log viewer-only stop and set its existing server event. No change to its journal inputs, provider calls, or lifecycle owner.
- `tests/test_quote_dashboard.py` and `tests/test_local_dashboard_server.py`: assert asset delivery and exercise the real local Dash Stop callback and port release. The latter test makes *loopback HTTP* requests to a synthetic viewer, never external requests.
- `src/mb_market_data/daily_runtime.py`: Q exits foreground display without stopping production. The attended `start --live` viewer returns to CMD only on a verified `final_status` and terminal completed/failed status; explicit `watch` remains persistent until Q/Ctrl+C.
- `tests/test_daily_viewer_lifecycle.py`: synthetic controller-status regression tests (no real controller or Schwab).

## Windows qualification — separate checkout only

Use a separate **Anaconda CMD** on MasterBot, with the existing `dashing` environment and an isolated checkout of `checkpoint/two-viewer-lifecycle-fixes-20261125`. No password is needed. Do **not** run `daily.cmd start --live`; these fixes are verified from fake status and local viewer behavior.

```cmd
conda activate dashing
```

```cmd
cd /d "%USERPROFILE%\\mb_viewer_lifecycle_qa_20261125"
```

```cmd
set PYTHONPATH=%CD%\\src;%CD%\\tests
```

```cmd
python -B -m unittest -v test_daily_viewer_lifecycle test_daily_runtime test_quote_dashboard
```

```cmd
python -B -m unittest -v test_local_dashboard_server
```

The last test uses localhost only, including one real Dash callback; it must return and release its temporary bound port. Both commands must exit zero. Record exact test counts and any skips/failures; do not assume results from the parent experimental branch apply to this selective backport.

### Short interactive Windows check

For **Stop server**, launch the existing historical Dashboard against any retained, completed journal **from this isolated checkout**, on an unused localhost port, without a Schwab connection. In Firefox, click the confirmed Stop server action. Verify that the browser displays **Overlay viewer stopped** and the viewer CMD returns, with no still-listening port. If the test is easier after next completed session, do not delay the synthetic qualification meanwhile.

For **Q**, run `python -B -m unittest -v test_daily_viewer_lifecycle` first. The real CMD keyboard's `msvcrt` behavior is the only manual Windows-specific input check not exercised by the synthetic callable; it can be checked with a temporary **synthetic** status file and `daily.cmd watch --state-root ...` in this isolated checkout. Never point the interactive viewer test at an active production session if not necessary.

## Release boundary

Windows qualification comes before requesting approval to promote. Promotion must review the exact changed-file list and must remain separate from deployment of the production polling installation. `main`, production processes, credentials, and existing journal evidence remain unchanged until expressly approved.
