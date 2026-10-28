#!/usr/bin/env python3
"""Check every signature against the public key the app ships with, then stage
exactly the files to publish.

Runs in the publish job, which has no key. It proves the signatures are good,
then copies only the signed index, its signature and the packs it lists into
--release, so nothing else that happens to be lying around gets published.

Usage:
  stage_release.py --dist dist --signed signed --public keys/food-pack-public.pem --release release
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_packs import sha256, shape  # noqa: E402


def verify(path: Path, signature_b64: str, public: Path) -> bool:
    try:
        raw = base64.b64decode(signature_b64.strip(), validate=True)
    except (binascii.Error, ValueError):
        return False
    with tempfile.NamedTemporaryFile() as sig:
        Path(sig.name).write_bytes(raw)
        result = subprocess.run(
            ["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(public), "-rawin", "-in", str(path), "-sigfile", sig.name],
            capture_output=True,
        )
        return result.returncode == 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", required=True, type=Path)
    ap.add_argument("--signed", required=True, type=Path)
    ap.add_argument("--public", required=True, type=Path)
    ap.add_argument("--release", required=True, type=Path)
    args = ap.parse_args()

    index_file = args.signed / "index.json"
    if not verify(index_file, (args.signed / "index.json.sig").read_text(), args.public):
        sys.exit("index.json signature doesn't verify")
    index = json.loads(index_file.read_text())
    unsigned = {k: v for k, v in index.items()}
    unsigned["packs"] = [{k: v for k, v in p.items() if k != "signature"} for p in index.get("packs", [])]
    problems = shape(unsigned)
    if problems:
        sys.exit("signed index has the wrong shape:\n  " + "\n  ".join(problems))

    args.release.mkdir(parents=True)
    for p in index["packs"]:
        f = args.dist / p["file"]
        if sha256(f) != p["sha256"] or not verify(f, p.get("signature", ""), args.public):
            sys.exit(f"{p['file']} doesn't match its signature")
        shutil.copyfile(f, args.release / p["file"])
    shutil.copyfile(index_file, args.release / "index.json")
    shutil.copyfile(args.signed / "index.json.sig", args.release / "index.json.sig")
    print(f"staged {len(index['packs'])} packs, all signatures good")
    print(f"version={index['version']}")


if __name__ == "__main__":
    main()
