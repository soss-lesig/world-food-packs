#!/usr/bin/env python3
"""Check and sign the packs built by build_packs.py.

Runs in its own job, after the build, with the signing key and nothing else:
it never reads the Open Food Facts download. It re-hashes every pack, checks
the sanity gates, signs each pack with Ed25519, then writes index.json and a
detached signature over it (index.json.sig).

Gates (any failure stops the release):
  - every pack's SHA-256 matches what the build recorded, and is under 2 GiB;
  - the reference country (default united-kingdom) has at least --min-uk
    products, and at least --min-coverage of the live count from Open Food
    Facts when that's available (--live-count);
  - no large country shrinks by more than 20% against the previous index.

Usage:
  sign_packs.py --dist dist --key key.pem [--previous previous-index.json]
                [--live-count N] [--reference united-kingdom] [--min-coverage 0.8] [--min-uk N]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_ASSET = 2 * 1024**3 - 1
LARGE_COUNTRY = 5000
MAX_SHRINK = 0.2


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sign(path: Path, key: Path) -> str:
    with tempfile.NamedTemporaryFile() as sig:
        subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-inkey", str(key), "-rawin", "-in", str(path), "-out", sig.name],
            check=True,
        )
        return base64.b64encode(Path(sig.name).read_bytes()).decode()


def gates(
    index: dict, dist: Path, previous: dict | None, live: int | None, reference: str, min_coverage: float, min_ref: int
) -> list[str]:
    problems = []
    packs = {p["country"]: p for p in index["packs"]}
    for p in index["packs"]:
        f = dist / p["file"]
        if not f.exists():
            problems.append(f"{p['file']} is missing")
            continue
        if f.stat().st_size > MAX_ASSET:
            problems.append(f"{p['file']} is over 2 GiB")
        if sha256(f) != p["sha256"]:
            problems.append(f"{p['file']} changed after it was built")
    ref = packs.get(reference)
    if ref is None:
        problems.append(f"no pack for {reference}")
    elif ref["product_count"] < min_ref:
        problems.append(f"{reference} has only {ref['product_count']} products (minimum {min_ref})")
    if ref is not None and live:
        coverage = ref["product_count"] / live
        print(f"{reference}: {ref['product_count']} of {live} live products ({coverage:.0%})")
        if coverage < min_coverage:
            problems.append(f"{reference} covers only {coverage:.0%} of Open Food Facts (minimum {min_coverage:.0%})")
    if previous:
        for p in previous.get("packs", []):
            now = packs.get(p["country"])
            before = p["product_count"]
            if before >= LARGE_COUNTRY and (now is None or now["product_count"] < before * (1 - MAX_SHRINK)):
                problems.append(f"{p['country']} shrank from {before} to {now['product_count'] if now else 0}")
    return problems


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", required=True, type=Path)
    ap.add_argument("--key", required=True, type=Path)
    ap.add_argument("--previous", type=Path)
    ap.add_argument("--live-count", type=int)
    ap.add_argument("--reference", default="united-kingdom")
    ap.add_argument("--min-coverage", type=float, default=0.8)
    ap.add_argument("--min-uk", type=int, default=0, help="absolute floor for the reference country")
    args = ap.parse_args()

    index = json.loads((args.dist / "index.unsigned.json").read_text())
    previous = json.loads(args.previous.read_text()) if args.previous and args.previous.exists() else None
    problems = gates(index, args.dist, previous, args.live_count, args.reference, args.min_coverage, args.min_uk)
    if problems:
        sys.exit("Not publishing:\n  " + "\n  ".join(problems))

    for p in index["packs"]:
        p["signature"] = sign(args.dist / p["file"], args.key)
    out = args.dist / "index.json"
    out.write_text(json.dumps(index, indent=2) + "\n")
    (args.dist / "index.json.sig").write_text(sign(out, args.key) + "\n")
    (args.dist / "index.unsigned.json").unlink()
    print(f"signed {len(index['packs'])} packs and the index")


if __name__ == "__main__":
    main()
