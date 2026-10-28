#!/usr/bin/env python3
"""Check every pack the build made, before anything is signed.

Runs in its own job with no secrets. It doesn't trust anything the build says
about itself: it opens every pack, counts the rows, checks the schema and the
values, and only then compares the counts with the index. If anything is off,
nothing gets signed.

Checks (any failure stops the release):
  - the index has exactly the expected fields and types, every country is a
    plain slug and every file is "<country>.sqlite.deflate";
  - dist holds only the index and the files it lists;
  - each file's size and SHA-256 match, and it's under 2 GiB;
  - each pack inflates to exactly its stated size, is a SQLite database with
    exactly the expected tables and indexes (no views, no triggers), passes
    SQLite's integrity check, and has the counts the index claims;
  - every value is in range: barcodes, text lengths, nutrition, flags;
  - the reference country (united-kingdom) has at least --min-uk products, at
    least --min-nutrition of them with nutrition, and at least --min-coverage of
    the live count from Open Food Facts when that's available;
  - no large country shrinks by more than 20%, and the reference country's
    share with nutrition doesn't drop by more than 10 points, against the
    previous release, which is required unless --first-release is given.

Usage:
  verify_packs.py --dist dist (--previous index.json | --first-release)
                  [--live-count-file live.json] --min-uk N
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
import zlib
from pathlib import Path

SCHEMA_VERSION = 3
SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
HEX64 = re.compile(r"[0-9a-f]{64}")
MAX_ASSET = 2 * 1024**3 - 1
MAX_UNPACKED = 1024**3  # no country comes close; a bigger claim is a red flag
LARGE_COUNTRY = 5000
MAX_SHRINK = 0.2
MAX_NUTRITION_DROP = 0.10

INDEX_FIELDS = {
    "schema_version": int, "version": int, "built_at": str, "source": str, "licence": str,
    "source_file": str, "source_sha256": str, "compression": str, "signature_algorithm": str, "packs": list,
}
PACK_FIELDS = {
    "country": str, "name": str, "file": str, "product_count": int, "products_with_nutrition": int,
    "size": int, "uncompressed_size": int, "sha256": str,
}
# Everything a pack may contain. FTS5 makes the shadow tables itself.
EXPECTED_OBJECTS = {
    ("table", "meta"), ("index", "sqlite_autoindex_meta_1"),
    ("table", "products"), ("index", "sqlite_autoindex_products_1"),
    ("table", "products_fts"), ("table", "products_fts_data"), ("table", "products_fts_idx"),
    ("table", "products_fts_docsize"), ("table", "products_fts_config"),
}
# Each query counts rows that break a rule; every one must come back 0.
VALUE_RULES = {
    "barcode not 8-14 digits": "length(barcode) NOT BETWEEN 8 AND 14 OR barcode GLOB '*[^0-9]*'",
    "name length": "length(name) NOT BETWEEN 2 AND 150",
    "brand length": "length(brand) > 100",
    "quantity or serving length": "length(quantity) > 60 OR length(serving_size) > 60",
    "serving size": "serving_g IS NOT NULL AND serving_g NOT BETWEEN 0 AND 5000",
    "flags": "has_nutrition NOT IN (0, 1) OR per_ml NOT IN (0, 1)",
    "nutrition without energy": "has_nutrition = 1 AND kcal IS NULL",
    "nutrition when flagged off": "has_nutrition = 0 AND (kcal IS NOT NULL OR protein IS NOT NULL OR fat IS NOT NULL)",
    "energy range": "kcal NOT BETWEEN 0 AND 900",
    "macro range": " OR ".join(f"{n} NOT BETWEEN 0 AND 100" for n in ("protein", "carbs", "sugar", "fat", "fibre", "salt")),
    # The build allows 105 before rounding to two places, so allow for rounding here.
    "macros add up": "coalesce(protein, 0) + coalesce(carbs, 0) + coalesce(fat, 0) > 105.05",
    "scans": "scans < 0",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def shape(index: object) -> list[str]:
    """The index must be exactly what build_packs.py writes, nothing more or less."""
    if not isinstance(index, dict) or set(index) != set(INDEX_FIELDS):
        return ["index fields are not the expected set"]
    problems = [f"index.{k} is not {t.__name__}" for k, t in INDEX_FIELDS.items() if type(index[k]) is not t]
    if problems:
        return problems
    if index["schema_version"] != SCHEMA_VERSION:
        problems.append(f"schema_version {index['schema_version']} (expected {SCHEMA_VERSION})")
    if not HEX64.fullmatch(index["source_sha256"]):
        problems.append("source_sha256 is not a SHA-256")
    if not 20260101 <= index["version"] <= 29991231:
        problems.append("version is not a date")
    seen = set()
    for i, p in enumerate(index["packs"]):
        if not isinstance(p, dict) or set(p) != set(PACK_FIELDS):
            problems.append(f"pack {i} fields are not the expected set")
            continue
        bad = [k for k, t in PACK_FIELDS.items() if type(p[k]) is not t]
        if bad:
            problems.append(f"pack {i} has wrong types: {bad}")
            continue
        c = p["country"]
        if not SLUG.fullmatch(c) or len(c) > 60:
            problems.append(f"pack {i} country {c!r} is not a plain slug")
        if p["file"] != f"{c}.sqlite.deflate":
            problems.append(f"pack {i} file {p['file']!r} doesn't match its country")
        if c in seen:
            problems.append(f"{c} is listed twice")
        seen.add(c)
        if not HEX64.fullmatch(p["sha256"]):
            problems.append(f"{c} sha256 is not a SHA-256")
        if not (0 < p["size"] <= MAX_ASSET and 0 < p["uncompressed_size"] <= MAX_UNPACKED):
            problems.append(f"{c} sizes are out of range")
        if not 0 <= p["products_with_nutrition"] <= p["product_count"]:
            problems.append(f"{c} counts don't add up")
        if len(p["name"]) > 80 or any(not ch.isprintable() for ch in p["name"]):
            problems.append(f"{c} display name is odd")
    return problems


def inflate(src: Path, dst: Path, expected: int) -> bool:
    """Unpack raw DEFLATE, refusing to write a byte more than the index says."""
    d = zlib.decompressobj(wbits=-15)
    written = 0
    with src.open("rb") as fin, dst.open("wb") as fout:
        for chunk in iter(lambda: fin.read(1 << 20), b""):
            data = d.decompress(chunk, expected - written + 1)
            while True:
                written += len(data)
                if written > expected:
                    return False
                fout.write(data)
                if not d.unconsumed_tail:
                    break
                data = d.decompress(d.unconsumed_tail, expected - written + 1)
        written += len(d.flush())
    return d.eof and written == expected


def check_pack(p: dict, dist: Path) -> list[str]:
    f = dist / p["file"]
    c = p["country"]
    if f.stat().st_size != p["size"]:
        return [f"{c} size doesn't match"]
    if sha256(f) != p["sha256"]:
        return [f"{c} changed after it was built"]
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pack.sqlite"
        if not inflate(f, db, p["uncompressed_size"]):
            return [f"{c} doesn't unpack to its stated size"]
        if db.read_bytes()[:16] != b"SQLite format 3\x00":
            return [f"{c} isn't a SQLite database"]
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            con.execute("PRAGMA trusted_schema = OFF")
            objects = set(con.execute("SELECT type, name FROM sqlite_schema").fetchall())
            if objects != EXPECTED_OBJECTS:
                return [f"{c} has unexpected database objects: {sorted(objects ^ EXPECTED_OBJECTS)}"]
            problems = []
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                problems.append(f"{c} fails SQLite's integrity check")
            if con.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                problems.append(f"{c} has the wrong schema version")
            count, with_nutrition = con.execute("SELECT count(*), coalesce(sum(has_nutrition), 0) FROM products").fetchone()
            if (count, with_nutrition) != (p["product_count"], p["products_with_nutrition"]):
                problems.append(f"{c} has {count} products ({with_nutrition} with nutrition), not what the index says")
            for rule, where in VALUE_RULES.items():
                bad = con.execute(f"SELECT count(*) FROM products WHERE {where}").fetchone()[0]
                if bad:
                    problems.append(f"{c}: {bad} rows break '{rule}'")
            meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
            if meta.get("country") != c:
                problems.append(f"{c} meta says it's {meta.get('country')!r}")
            return problems
        finally:
            con.close()


def gates(index: dict, previous: dict | None, live: int | None, args) -> list[str]:
    problems = []
    packs = {p["country"]: p for p in index["packs"]}
    ref = packs.get(args.reference)
    if ref is None:
        return [f"no pack for {args.reference}"]
    count, nut = ref["product_count"], ref["products_with_nutrition"]
    share = nut / count if count else 0
    print(f"{args.reference}: {count} products, {share:.0%} with nutrition")
    if count < args.min_uk:
        problems.append(f"{args.reference} has only {count} products (minimum {args.min_uk})")
    if share < args.min_nutrition:
        problems.append(f"{args.reference}: only {share:.0%} have nutrition (minimum {args.min_nutrition:.0%})")
    if live:
        coverage = count / live
        print(f"{args.reference}: {count} of {live} live products ({coverage:.0%})")
        if coverage < args.min_coverage:
            problems.append(f"{args.reference} covers only {coverage:.0%} of Open Food Facts (minimum {args.min_coverage:.0%})")
    else:
        # Not silent: GitHub shows this on the run's summary page.
        print("::warning::Open Food Facts' live count wasn't available, so the coverage gate didn't run this week")
    if previous:
        for p in previous.get("packs", []):
            now = packs.get(p.get("country"))
            before = p.get("product_count", 0)
            if before >= LARGE_COUNTRY and (now is None or now["product_count"] < before * (1 - MAX_SHRINK)):
                problems.append(f"{p.get('country')} shrank from {before} to {now['product_count'] if now else 0}")
            if p.get("country") == args.reference and before:
                old_share = p.get("products_with_nutrition", 0) / before
                if share < old_share - MAX_NUTRITION_DROP:
                    problems.append(f"{args.reference} nutrition share fell from {old_share:.0%} to {share:.0%}")
    return problems


def live_count(path: Path | None) -> int | None:
    """The total from Open Food Facts' search API, if it gave a sensible one."""
    if path is None or not path.exists():
        return None
    try:
        n = json.loads(path.read_text()).get("count")
    except (ValueError, AttributeError):
        return None
    if isinstance(n, str) and n.isascii() and n.isdigit():
        n = int(n)
    return n if type(n) is int and 0 < n < 10**9 else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", required=True, type=Path)
    prev = ap.add_mutually_exclusive_group(required=True)
    prev.add_argument("--previous", type=Path, help="the last published index.json, already signature-checked")
    prev.add_argument("--first-release", action="store_true", help="there is no previous release to compare with")
    ap.add_argument("--live-count-file", type=Path)
    ap.add_argument("--reference", default="united-kingdom")
    ap.add_argument("--min-uk", type=int, required=True)
    ap.add_argument("--min-coverage", type=float, default=0.8)
    ap.add_argument("--min-nutrition", type=float, default=0.6)
    args = ap.parse_args()

    index_path = args.dist / "index.unsigned.json"
    index = json.loads(index_path.read_text())
    problems = shape(index)
    if problems:
        sys.exit("Not publishing:\n  " + "\n  ".join(problems))

    listed = {p["file"] for p in index["packs"]} | {index_path.name}
    present = {f.name for f in args.dist.iterdir()}
    if present != listed or any(not (args.dist / n).is_file() or (args.dist / n).is_symlink() for n in present):
        sys.exit(f"Not publishing: dist doesn't hold exactly the listed files: {sorted(present ^ listed)}")

    for p in index["packs"]:
        problems += check_pack(p, args.dist)
    previous = json.loads(args.previous.read_text()) if args.previous else None
    problems += gates(index, previous, live_count(args.live_count_file), args)
    if problems:
        sys.exit("Not publishing:\n  " + "\n  ".join(problems))
    print(f"verified {len(index['packs'])} packs")


if __name__ == "__main__":
    main()
