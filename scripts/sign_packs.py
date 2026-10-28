#!/usr/bin/env python3
"""Sign packs that verify_packs.py has already checked.

Runs in its own job with the signing key and nothing else: no network calls, no
write access to the repo, nothing from Open Food Facts except the pack files and
the index, which it treats as untrusted bytes. Before signing it re-checks the
index's shape, that every country is a plain slug, that dist holds exactly the
listed files, and every file's size and SHA-256, so it signs exactly what was
verified and nothing else.

With --expect-index-sha256 (always, in the workflow) it first checks the index is
byte for byte the one the verify job passed. The index pins every pack's hash, so
nothing swapped in between verify and sign can be signed.

Writes index.json and its detached signature (index.json.sig) to --out. The
packs themselves aren't changed; each pack's signature goes in the index.

Usage:
  sign_packs.py --dist dist --key key.pem --out signed
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# Scripts run with python -I, which leaves their own folder off the import path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_packs import sha256, shape  # noqa: E402


def sign(path: Path, key: Path) -> str:
    with tempfile.NamedTemporaryFile() as sig:
        subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-inkey", str(key), "-rawin", "-in", str(path), "-out", sig.name],
            check=True,
        )
        return base64.b64encode(Path(sig.name).read_bytes()).decode()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", required=True, type=Path)
    ap.add_argument("--key", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--expect-index-sha256", required=True, help="SHA-256 of the index the verify job passed")
    args = ap.parse_args()

    index_path = args.dist / "index.unsigned.json"
    if sha256(index_path) != args.expect_index_sha256.strip().lower():
        sys.exit("Not signing: the index isn't the one that was verified")
    index = json.loads(index_path.read_text())
    problems = shape(index)
    if not problems:
        listed = {p["file"] for p in index["packs"]} | {index_path.name}
        present = {f.name for f in args.dist.iterdir()}
        if present != listed:
            problems.append(f"dist doesn't hold exactly the listed files: {sorted(present ^ listed)}")
    if not problems:
        for p in index["packs"]:
            f = args.dist / p["file"]
            if f.is_symlink() or not f.is_file() or f.stat().st_size != p["size"] or sha256(f) != p["sha256"]:
                problems.append(f"{p['file']} isn't the file that was verified")
    if problems:
        sys.exit("Not signing:\n  " + "\n  ".join(problems))

    for p in index["packs"]:
        p["signature"] = sign(args.dist / p["file"], args.key)
    args.out.mkdir(parents=True, exist_ok=True)
    out = args.out / "index.json"
    out.write_text(json.dumps(index, indent=2) + "\n")
    (args.out / "index.json.sig").write_text(sign(out, args.key) + "\n")
    print(f"signed {len(index['packs'])} packs and the index")


if __name__ == "__main__":
    main()
