"""Check the extracted report against native synthetic journals only."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest


def main():
    parser = argparse.ArgumentParser(description="Offline report verification; synthetic data only.")
    parser.add_argument("--market-data-root", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    repository = args.market_data_root or root.parent.parent
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        for relative, expected in manifest["files_sha256"].items():
            path = root / relative
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f"Packet file differs: {relative}")
        if not (repository / "src" / "mb_market_data" / "quote_observation_store.py").is_file():
            raise ValueError("Extract under mb_market_data\\output, or supply --market-data-root.")
        sys.path.insert(0, str(repository / "src"))
        import mb_market_data
        spec = importlib.util.spec_from_file_location("mb_market_data.membership_report", root / "membership_report.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        sys.path.insert(0, str(root / "tests"))
        suite = unittest.defaultTestLoader.loadTestsFromNames([
            "test_membership_report", "test_membership_report_reasons",
        ])
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        passed = result.wasSuccessful() and not result.skipped
        print(json.dumps({
            "contract": "membership-changes-offline-verification-v1",
            "result": "PASS" if passed else "FAILED", "tests": result.testsRun,
            "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
            "synthetic_journals_only": True, "production_journals_opened": False,
            "api_or_password_required": False, "source_commit": manifest["source_commit"],
        }, indent=2))
        return 0 if passed else 1
    except (OSError, ValueError, ImportError) as exc:
        print(f"Report verification ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
