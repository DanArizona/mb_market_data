"""Verify a candidate in isolation; install reviewed source only after the close."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PACKAGE = Path(__file__).resolve().parent


def digest(path):
    content = path.read_bytes()
    if path.suffix in {".py", ".cmd", ".json", ".md", ".css", ".toml", ".txt", ".js", ".svg"}:
        content = content.replace(b"\r\n", b"\n")
    return hashlib.sha256(content).hexdigest()


def load_package():
    manifest = json.loads((PACKAGE / "DEPLOYMENT.json").read_text())
    for relative, expected in manifest["sha256"].items():
        path = PACKAGE / relative
        if not path.resolve().is_relative_to(PACKAGE) or digest(path) != expected:
            raise ValueError("Candidate package changed: " + relative)
    return manifest


def run(command, *, cwd=None, env=None):
    subprocess.run(command, cwd=cwd, env=env, check=True)


def verify(root, manifest):
    candidate = PACKAGE / "candidate"
    report = root / "output/polling_resilience_verification" / datetime.now(timezone.utc).strftime("synthetic_%Y%m%dT%H%M%S%fZ")
    report.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(candidate / "src")
    print("SYNTHETIC ONLY. No API, password, production journal, or production process changes.", flush=True)
    run([sys.executable, str(PACKAGE / "run_contract_tests.py")], env=env)
    run([sys.executable, str(candidate / "probes/verify_polling_write_recovery.py"),
         "--report-root", str(report)], env=env)
    run([sys.executable, str(PACKAGE / "tools/manual_quote_recovery_v1/verify_recovery.py"),
         "--market-data-root", str(candidate)], env=env)
    run([sys.executable, str(PACKAGE / "tools/polling_health_recovery_v1/run_packet.py"),
         "verify", "--market-data-root", str(candidate)], env=env)
    marker = {"windows_accepted": os.name == "nt", "verified_at_utc": datetime.now(timezone.utc).isoformat(),
              "package_sha256": digest(PACKAGE / "DEPLOYMENT.json"), "source_commit": manifest["source_commit"],
              "report": str(report)}
    (PACKAGE / "ACCEPTED.json").write_text(json.dumps(marker, indent=2))
    (report / "acceptance.json").write_text(json.dumps(marker, indent=2))
    print("Polling resilience candidate verification: PASS", flush=True)
    print("Production polling and its current health monitor were unchanged.")
    print(f"Verification report: {report}")


def validate_idle(root):
    if os.name != "nt":
        raise ValueError("Installation is restricted to the verified Windows production host")
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.hour < 16:
        raise ValueError("Install after 16:00 ET, after the daily audit and stopping prod-health")
    sys.path.insert(0, str(PACKAGE / "tools/polling_health_recovery_v1"))
    import acquisition_health
    inventory = acquisition_health.windows_process_inventory()
    if not inventory["known"]:
        raise ValueError("Installation refused: process inspection is unknown: " + str(inventory["error"]))
    for process in inventory["processes"]:
        argv = process["argv"]
        names = {Path(arg.replace("\\", "/")).name.lower() for arg in argv}
        if "probe_universe_quote_watch.py" in names:
            raise ValueError(f"A poller is still running (PID {process['pid']}); leave it running and install later")
        if names & {"polling_health.py", "run_packet.py"} and "monitor" in argv:
            raise ValueError(f"Stop prod-health with Q before installing (PID {process['pid']})")


def install(root, manifest):
    validate_idle(root)
    marker = json.loads((PACKAGE / "ACCEPTED.json").read_text())
    if not marker.get("windows_accepted") or marker.get("package_sha256") != digest(PACKAGE / "DEPLOYMENT.json"):
        raise ValueError("Run 01_prod_main_offline_verify.cmd successfully on Windows first")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if head != manifest["baseline_commit"]:
        raise ValueError("Installation requires the reviewed baseline commit; found " + head)
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True)
    if dirty.strip():
        raise ValueError("Installation refused: Git working tree has changes; retain them for review")
    for relative, expected in manifest["baseline_sha256"].items():
        if digest(root / relative) != expected:
            raise ValueError("Baseline source changed: " + relative)
    # Source guards and all native candidate checks precede the first mutation.
    receipt_dir = root / "output/polling_resilience_install" / datetime.now(timezone.utc).strftime("install_%Y%m%dT%H%M%S%fZ")
    receipt_dir.mkdir(parents=True, exist_ok=False)
    receipt = {"before_commit": head, "after_commit": manifest["source_commit"], "state": "PREPARED", "backups": []}
    receipt_path = receipt_dir / "installation.json"
    receipt_path.write_text(json.dumps(receipt, indent=2))
    for name in ("polling_health_recovery_v1", "manual_quote_recovery_v1"):
        existing = root / "output" / name
        if existing.exists():
            expected = manifest["existing_tools_sha256"][name]
            for relative, fingerprint in expected.items():
                path = existing / relative
                if not path.exists() or digest(path) != fingerprint:
                    raise ValueError("Existing tool folder differs from the reviewed copy: " + str(path))
            backup = receipt_dir / name
            shutil.copytree(existing, backup)
            receipt["backups"].append(str(backup))
    shutil.copyfile(PACKAGE / "polling-resilience.bundle", receipt_dir / "polling-resilience.bundle")
    try:
        run(["git", "bundle", "verify", str(PACKAGE / "polling-resilience.bundle")], cwd=root)
        run(["git", "fetch", str(PACKAGE / "polling-resilience.bundle"), "feature/acquisition-health-recovery"], cwd=root)
        run(["git", "merge", "--ff-only", manifest["source_commit"]], cwd=root)
        receipt["state"] = "SOURCE_INSTALLED"
        receipt_path.write_text(json.dumps(receipt, indent=2))
        for relative, expected in manifest["candidate_source_sha256"].items():
            if digest(root / relative) != expected:
                raise ValueError("Installed source verification failed: " + relative)
        for name in ("polling_health_recovery_v1", "manual_quote_recovery_v1"):
            destination = root / "output" / name
            destination.mkdir(exist_ok=True)
            for path in (PACKAGE / "tools" / name).iterdir():
                if path.is_file():
                    shutil.copyfile(path, destination / path.name)
        receipt["state"] = "INSTALLED"
        receipt_path.write_text(json.dumps(receipt, indent=2))
        print("Polling resilience installation: PASS", flush=True)
        print("Daily journals and acquired evidence were unchanged. No API call or poller launch occurred.")
        print("Next production session: use the normal poller commands and 02_prod_health_monitor.cmd.")
        print(f"Installation receipt and preserved previous tools: {receipt_dir}")
    except Exception as error:
        receipt.update(state="INCOMPLETE", error=f"{type(error).__name__}: {error}")
        receipt_path.write_text(json.dumps(receipt, indent=2))
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("verify", "install"))
    parser.add_argument("--market-data-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = load_package()
        {"verify": verify, "install": install}[args.operation](args.market_data_root.resolve(), manifest)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Polling resilience STOPPED: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        raise SystemExit(2)
