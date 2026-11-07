"""Reconstruct the unchanged uptime candidate in a NEW folder; no live operations.

Reads only explicitly inventoried files from the original November 6 candidate.
Does not copy its output, edit its files, install packages, or execute the result.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import lzma
from pathlib import Path, PurePosixPath
import sys

PAYLOAD_SHA256 = '32e4420dc16127c4d7eec2944c3b04f791d92b28a1d5c894c880a1dd22169b72'
TARGET_MANIFEST_SHA256 = '002ebdfc86fa470baa5a39eab86e0d0ca74f548a89e0f1075e143ae6943e3d7b'
TARGET_FILES = 129


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def relative(name: str) -> Path:
    p = PurePosixPath(name)
    if (not isinstance(name, str) or not name or p.is_absolute()
            or any(part in ('', '.', '..') for part in name.split('/'))
            or '\\' in name or ':' in name):
        raise ValueError('Unsafe delivery path')
    return Path(*p.parts)


def restore(base: Path, destination: Path, payload_path: Path) -> None:
    base = base.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve()
    if not base.is_dir():
        raise ValueError('Original extracted candidate directory is required')
    if destination.exists():
        raise FileExistsError(f'Destination already exists; nothing overwritten: {destination}')
    if destination.is_relative_to(base) or base.is_relative_to(destination):
        raise ValueError('Destination must be separate from the original candidate')
    encoded = ''.join(
        payload_path.with_name(f'payload{index:02}.b64').read_text(encoding='ascii').strip()
        for index in range(1, 7)
    )
    packed = base64.b64decode(encoded, validate=True)
    if digest(packed) != PAYLOAD_SHA256:
        raise ValueError('GitHub delivery payload checksum differs; stop')
    recipe = json.loads(lzma.decompress(packed))
    if recipe.get('contract') != 'exact-package-reconstruction-v1':
        raise ValueError('Unsupported delivery contract')
    files = recipe['files']
    inventory = {name: entry['sha256'] for name, entry in files.items()}
    inventory_bytes = json.dumps(inventory, sort_keys=True, separators=(',', ':')).encode()
    if len(files) != TARGET_FILES or digest(inventory_bytes) != TARGET_MANIFEST_SHA256:
        raise ValueError('Target package inventory differs; stop')
    # Verify all inputs and build all output bytes BEFORE creating any folders.
    sources = {}
    for name, expected in recipe['base_files'].items():
        path = base / relative(name)
        if path.is_symlink() or not path.resolve().is_relative_to(base):
            raise ValueError(f'Original file must remain inside its candidate: {name}')
        data = path.read_bytes()
        if digest(data) != expected:
            raise ValueError(f'Original file differs; no output written: {name}')
        sources[name] = data
    assembled = {}
    for name, entry in files.items():
        relative(name)
        if 'text' in entry:
            data = entry['text'].encode('utf-8')
        elif 'lines' not in entry:
            data = sources[entry['base']]
        else:
            lines = sources[entry['base']].decode('utf-8').splitlines(keepends=True)
            chunks = []
            for operation in entry['lines']:
                if isinstance(operation, str):
                    chunks.append(operation)
                else:
                    first, last = operation
                    if not 0 <= first <= last <= len(lines):
                        raise ValueError('Invalid reconstruction range')
                    chunks.append(''.join(lines[first:last]))
            data = ''.join(chunks).encode('utf-8')
        if digest(data) != entry['sha256']:
            raise ValueError(f'Reconstructed bytes differ; no output written: {name}')
        assembled[name] = data
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(exist_ok=False)
    for name, data in assembled.items():
        path = destination / relative(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(data)
        if digest(path.read_bytes()) != inventory[name]:
            raise OSError(f'Written file checksum differs: {name}')
    print(f'DELIVERY VERIFIED: {len(assembled)}/{TARGET_FILES} exact candidate files')
    print(f'Candidate: {destination}')
    print('Original files and output unchanged. No credentials, provider calls, or installation.')
    print('Windows verification is still required; the candidate was not executed.')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True, type=Path)
    parser.add_argument('--destination', required=True, type=Path)
    args = parser.parse_args()
    try:
        restore(args.base, args.destination, Path(__file__).with_name('payload.xz'))
        return 0
    except Exception as error:
        print(f'DELIVERY BLOCKED: {type(error).__name__}: {error}', file=sys.stderr)
        print('Do not delete or overwrite the original candidate or its output.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
