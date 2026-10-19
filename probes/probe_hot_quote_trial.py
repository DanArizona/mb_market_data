"""Explicitly opt into a bounded isolated Schwab or synthetic Hot timing trial."""
from __future__ import annotations

import argparse
import getpass
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from mb_market_data.hot_provider_trial import (
    SyntheticClient, TrialSettings, prepare_schwab_client, run_trial, validate_output_path,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--synthetic", action="store_true")
    source.add_argument("--schwab", action="store_true")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--intervals", nargs="+", type=int, required=True)
    parser.add_argument("--phase-seconds", type=int, required=True)
    parser.add_argument("--quote-call-budget", type=int, required=True)
    parser.add_argument("--request-timeout-seconds", type=int, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--ecfg", type=Path)
    parser.add_argument("--verification-mode", choices=("full_history", "guarded_incremental"), default="full_history")
    args = parser.parse_args(argv)
    client = None
    rejection = None
    preparation_seconds = None
    try:
        settings = TrialSettings(tuple(args.symbols), tuple(args.intervals), args.phase_seconds,
            args.quote_call_budget, args.request_timeout_seconds, "schwab" if args.schwab else "synthetic")
        settings.check_session(datetime.now(timezone.utc))
        path = args.output_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
        validate_output_path(path)
        if args.schwab and (args.ecfg is None or not args.ecfg.is_file()):
            raise ValueError("Provide the existing encrypted .ecfg path")
        print(f"Isolated Hot trial: {settings.source}; {' '.join(settings.symbols)}; intervals={settings.intervals}; {settings.phase_seconds}s per phase", flush=True)
        print(f"At most {settings.planned_calls} planned quote calls; hard quote-call budget={settings.quote_call_budget}; request timeout={settings.request_timeout_seconds}s", flush=True)
        print("Fresh trial journals only. Client authentication/refresh precedes timing. Ctrl+C ends this trial.", flush=True)
        if args.schwab:
            password = getpass.getpass("Encrypted config password: ")
            startup = time.perf_counter()
            try:
                client, rejection = prepare_schwab_client(args.ecfg, password, settings)
            finally:
                password = None
            preparation_seconds = time.perf_counter() - startup
            print(f"Credential/client preparation completed in {preparation_seconds:.3f}s", flush=True)
        else:
            client = SyntheticClient()
        report = run_trial(path, settings, client, verification_mode=args.verification_mode, auth_rejection=rejection, client_preparation_seconds=preparation_seconds, on_status=lambda s: print(s, flush=True))
        print((path / "summary.txt").read_text(encoding="utf-8"), flush=True)
        print("Trial finished; command returns to its prompt.", flush=True)
        return 130 if report["interrupted"] else (0 if report["passed"] else 1)
    except KeyboardInterrupt:
        print("Trial startup interrupted; no further quote calls.", flush=True)
        return 130
    except Exception as exc:
        # Deliberately exclude config values, SDK exception messages and URLs.
        print(f"Trial could not complete ({type(exc).__name__}). Check config path/password and mb-schwab-auth separately; do not paste secrets.", flush=True)
        return 1
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                print("Client close reported an error; this trial has ended.", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
