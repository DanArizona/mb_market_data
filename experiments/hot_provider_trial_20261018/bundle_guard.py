"""Check bundled payload bytes before importing trial code. This is not a signature."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path, PurePosixPath


def verify(root):
    root = Path(root).resolve()
    manifest = json.loads((root / "experiments/hot_provider_trial_20261018/source_manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        name = entry["path"]
        relative = PurePosixPath(name)
        if not isinstance(name, str) or "\\" in name or relative.is_absolute() or ".." in relative.parts or ":" in name:
            raise ValueError("Manifest path is outside the bundle")
        path = root.joinpath(*relative.parts).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Manifest file is missing or outside the bundle")
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("Bundled file differs: " + name)
    if not manifest["files"]:
        raise ValueError("Empty source manifest")
    return manifest
