"""Build a new immutable standalone report ZIP from the reviewed source."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-commit", help="Published source commit, when available")
    parser.add_argument("--base-commit", help="Existing source baseline; not the identity of unpublished edits")
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    helper = Path(__file__).resolve().parent
    output = args.output_root / "membership_report_20261028"
    output.mkdir(parents=True, exist_ok=False)
    paths = {
        "membership_report.py": source / "src/mb_market_data/membership_report.py",
        "report_membership.cmd": helper / "report_membership.cmd",
        "verify_membership.cmd": helper / "verify_membership.cmd",
        "verify.py": helper / "verify.py",
        "README.md": helper / "README.md",
        "report_details.md": source / "docs/symbol_membership_report.md",
        "tests/test_membership_report.py": source / "tests/test_membership_report.py",
        "tests/test_membership_report_reasons.py": source / "tests/test_membership_report_reasons.py",
    }
    for relative, path in paths.items():
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = path.read_bytes()
        if relative.endswith(".cmd"):
            payload = payload.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        destination.write_bytes(payload)
    probe = output / "probes/report_symbol_membership.py"
    probe.parent.mkdir()
    probe.write_text('"""Run the exact standalone report during offline CLI verification."""\n'
                     'import sys\nfrom pathlib import Path\n'
                     'sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n'
                     'from membership_report import main\n'
                     'if __name__ == "__main__":\n    raise SystemExit(main())\n', encoding="utf-8")
    inventory = {str(path.relative_to(output)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in sorted(output.rglob("*")) if path.is_file()}
    manifest = {"contract": "standalone-membership-report-packet-v2", "source_commit": args.source_commit,
                "base_commit": args.base_commit,
                "source_identity": "published commit" if args.source_commit else "local update identified by files_sha256; not published",
                "created_at_utc": datetime.now(timezone.utc).isoformat(), "files_sha256": inventory,
                "production_changes": False, "actual_symbol_exclusion_reasons_inferred": False}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    archive = args.output_root / (output.name + ".zip")
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                bundle.write(path, (Path(output.name) / path.relative_to(output)).as_posix())
    print(json.dumps({"archive": str(archive.resolve()), "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                      "files": len(inventory) + 1, "source_commit": args.source_commit}, indent=2))


if __name__ == "__main__":
    main()
