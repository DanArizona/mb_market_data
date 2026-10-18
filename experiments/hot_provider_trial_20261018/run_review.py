"""Pinned payload launcher; no installation and no production journal arguments."""
from __future__ import annotations
import importlib.util
import json
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
from bundle_guard import verify


class Tee:
    def __init__(self, *streams): self.streams = streams
    def write(self, text):
        for stream in self.streams: stream.write(text); stream.flush()
    def flush(self):
        for stream in self.streams: stream.flush()


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("verify", "schwab"):
        print("Use the fixed offline verification or Schwab trial CMD launcher.", flush=True)
        return 2
    try:
        manifest = verify(ROOT)
    except Exception as exc:
        print("Bundle verification FAILED: " + str(exc), flush=True)
        return 1
    sys.path.insert(0, str(ROOT/"src"))
    import mb_market_data.sampling_acquisition as acquisition
    expected = ROOT/"src/mb_market_data/sampling_acquisition.py"
    if Path(acquisition.__file__).resolve() != expected.resolve():
        print("Unexpected acquisition import location", flush=True)
        return 1
    print("Source verified: " + str(expected), flush=True)
    print("Baseline: " + manifest["market_data_baseline_commit"], flush=True)
    if sys.argv[1] == "verify":
        print("OFFLINE HOT INTEGRATION: tests, then short actual-clock synthetic phases; no API or password.", flush=True)
        results = ROOT/"runs"/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-verify-"+uuid.uuid4().hex[:8])
        results.mkdir(parents=True,exist_ok=False)
        loader = unittest.TestLoader()
        suite = unittest.TestSuite(loader.discover(str(ROOT/"tests"), pattern=pattern) for pattern in
            ("test_sampling_acquisition.py", "test_hot_provider_trial.py", "test_hot_trial_bundle.py"))
        with (results/"tests.txt").open("w",encoding="utf-8") as file:
            passed = unittest.TextTestRunner(stream=Tee(sys.stdout,file),verbosity=2).run(suite).wasSuccessful()
        if not passed:
            print("Offline verification FAILED; tests: " + str(results),flush=True)
            return 1
        print("Tests passed. Starting two bounded synthetic phases (about 10 seconds including alignment).",flush=True)
        from mb_market_data.hot_provider_trial import SyntheticClient, TrialSettings, run_trial
        report = run_trial(results/"synthetic_trial", TrialSettings(("AAPL","MSFT","SPY"),(2,4),4,3,3,"synthetic"),
            SyntheticClient(), on_status=lambda s:print(s,flush=True))
        print((results/"synthetic_trial/summary.txt").read_text(encoding="utf-8"),flush=True)
        print("Offline Hot integration verification: " + ("PASS" if report["passed"] else "FAILED"),flush=True)
        print("Finished. This command returns to its prompt.",flush=True)
        return 0 if report["passed"] else 1
    print("SCHWAB HOT TIMING: AAPL/MSFT/SPY; 60s at 2s, then 60s at 4s; at most 45 quote calls.",flush=True)
    print("One password prompt. About two minutes plus setup; no production data/source changes.",flush=True)
    spec = importlib.util.spec_from_file_location("hot_trial_probe",ROOT/"probes/probe_hot_quote_trial.py")
    probe = importlib.util.module_from_spec(spec); spec.loader.exec_module(probe)
    return probe.main(["--schwab","--symbols","AAPL","MSFT","SPY","--intervals","2","4",
        "--phase-seconds","60","--quote-call-budget","45","--request-timeout-seconds","3",
        "--output-root",str(ROOT/"runs/schwab_trials"),"--ecfg",r"C:\Users\danla\MBV\secure_schwabdev.ecfg"])


if __name__ == "__main__":
    raise SystemExit(main())
