"""Verify accepted integration source without weakening checks for Windows Git CRLF.

Git may present LF source as CRLF in a Windows working checkout. Accept that
reversible line-ending conversion only when it exactly restores the accepted
source SHA-256. Other file-byte differences remain blocking errors.
"""

import hashlib
import json
import os
from pathlib import Path


def verify_integrated_sources(root, *, windows_checkout=None):
    root = Path(root)
    if windows_checkout is None:
        windows_checkout = os.name == "nt"
    inventory = json.loads(
        (root / "INTEGRATION_SOURCE_INVENTORY.json").read_text(encoding="utf-8")
    )
    normalized = 0
    for name, expected in inventory["files"].items():
        path = root / name
        if not path.is_file():
            raise ValueError("Integrated source differs: " + name)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() == expected:
            continue
        if (
            windows_checkout
            and b"\r\n" in raw
            and hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest() == expected
        ):
            normalized += 1
            continue
        raise ValueError("Integrated source differs: " + name)
    return {"file_count": len(inventory["files"]), "crlf_normalized": normalized}
