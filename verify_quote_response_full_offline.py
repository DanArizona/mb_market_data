"""Experimental full offline regression across the accepted core test modules.

Do not replace the immutable production fingerprint or its verifier. The
experimental quote handler is intentionally different, so this runner
reports source inventory verification as NOT PERFORMED.
"""
from __future__ import annotations

import builtins
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import socket
import sys
import unittest

from verify_offline import TESTS, EXPECTED_TESTS, Tee


ROOT = Path(__file__).resolve().parent
EXTRA_DIAGNOSTIC_TESTS = 4
EXPECTED = EXPECTED_TESTS + EXTRA_DIAGNOSTIC_TESTS


def denied(*_args, **_kwargs):
    raise AssertionError("No external network, provider access, or password prompts allowed")


def main() -> int:
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
    os.environ["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT / "tests")])
    output = ROOT / "output" / (
        "quote-response-full-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    )
    output.mkdir(parents=True, exist_ok=False)
    os.environ["MB_MEMBERSHIP_PROOF"] = str(output / "membership-proof.json")

    report = {
        "result": "BLOCKED",
        "expected_tests": EXPECTED,
        "baseline_expected_tests": EXPECTED_TESTS,
        "added_diagnostic_tests": EXTRA_DIAGNOSTIC_TESTS,
        "source_inventory_verified": False,
        "source_inventory_note": (
            "Intentional diagnostic source changes: original immutable "
            "fingerprint remains unchanged. No new production fingerprint "
            "or release approval is implied."
        ),
        "real_provider_calls": False,
        "password_prompts": False,
        "production_changes": False,
        "environment": sys.executable,
    }
    try:
        import exchange_calendars as xcals
        import mb_tools.schwab_secure  # verify installed dependency; no client

        socket.socket.connect = denied
        socket.socket.connect_ex = denied
        socket.create_connection = denied
        socket.getaddrinfo = denied
        getpass.getpass = denied
        builtins.input = denied
        print("OFFLINE EXPERIMENT. No SDK client, network, or passwords.")
        print("Calendar:", xcals.__version__)
        with (output / "console.txt").open("x", encoding="utf-8") as log:
            with redirect_stdout(Tee(sys.stdout, log)), redirect_stderr(Tee(sys.stderr, log)):
                suite = unittest.defaultTestLoader.loadTestsFromNames(TESTS)
                result = unittest.TextTestRunner(verbosity=2).run(suite)
        passed = result.wasSuccessful() and result.testsRun == EXPECTED and not result.skipped
        report.update(
            result="PASS" if passed else "FAIL",
            tests_run=result.testsRun,
            failures=len(result.failures),
            errors=len(result.errors),
            skipped=len(result.skipped),
            platform=sys.platform,
            calendar=xcals.__version__,
        )
    except Exception as error:
        report.update(error_type=type(error).__name__, detail=str(error))

    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    print("Evidence:", output)
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
