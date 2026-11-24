"""Offline verification for the experimental historical Overlay workspace layout.

The integration source inventory is intentionally not asserted: it fingerprints
the original pre-workspace integration and must remain an immutable provenance
record. This runner verifies the new source's behavior, not baseline identity.
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
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]


def denied(*_args, **_kwargs):
    raise AssertionError("No provider, external network, or credentials in Overlay tests")


def main() -> int:
    try:
        import dash
        import plotly
    except ImportError as exc:
        print("Overlay dependencies missing:", exc.name)
        print("Use the existing dashing environment on MasterBot.")
        return 2

    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.create_connection = denied
    socket.getaddrinfo = denied
    getpass.getpass = denied
    builtins.input = denied

    output = ROOT / "output" / (
        "overlay-workspace-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    )
    output.mkdir(parents=True)
    os.environ["MB_OVERLAY_PREVIEW"] = str(output / "synthetic-overlay-preview.html")
    modules = [
        "test_observation_overlay",
        "test_observation_overlay_view",
        "test_overlay_halts",
        "test_daily_overlay_render",
        "test_quote_dashboard",
        "test_quote_dashboard_replay",
        "test_overlay_indexed_selection",
        "test_membership_browser",
        "test_overlay_session_catalog",
        "test_overlay_live_attachment",
        "test_overlay_live_tail",
        "test_overlay_operator_controls",
    ]
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = result.wasSuccessful() and result.testsRun == 127 and not result.skipped
    report = {
        "result": "PASS" if passed else "FAIL",
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "dash": dash.__version__,
        "plotly": plotly.__version__,
        "real_provider_calls": False,
        "production_changes": False,
        "source_inventory_verified": False,
        "source_inventory_note": "Original integration fingerprint is preserved; changed workspace source is not byte-identical to that checkpoint.",
        "desktop_visual_verified": False,
        "synthetic_preview": str(output / "synthetic-overlay-preview.html"),
        "environment": sys.executable,
    }
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
