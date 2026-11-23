"""Offline, focused QA for experimental Schwab quote-response diagnostics.

Source fingerprint of the accepted production integration remains immutable.
This verifier tests the intentionally changed response parser without a
Schwab SDK client, network, credential prompt, or production journal access.
"""
from __future__ import annotations

import builtins
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import socket
import sys
import unittest


ROOT = Path(__file__).resolve().parent
EXPECTED = 15


def denied(*_args, **_kwargs):
    raise AssertionError("No network or password prompting in quote diagnostics QA")


def main() -> int:
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.create_connection = denied
    socket.getaddrinfo = denied
    getpass.getpass = denied
    builtins.input = denied

    suite = unittest.defaultTestLoader.loadTestsFromName("test_schwab_quotes")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = result.wasSuccessful() and result.testsRun == EXPECTED and not result.skipped
    report = {
        "result": "PASS" if passed else "FAIL",
        "tests_run": result.testsRun,
        "expected_tests": EXPECTED,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "real_provider_calls": False,
        "password_prompts": False,
        "production_changes": False,
        "source_inventory_verified": False,
        "source_inventory_note": (
            "This experiment intentionally changes a file from the immutable "
            "production integration fingerprint. Do not rewrite the baseline."
        ),
        "environment": sys.executable,
    }
    output = ROOT / "output" / (
        "quote-response-diagnostics-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    )
    output.mkdir(parents=True, exist_ok=False)
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
