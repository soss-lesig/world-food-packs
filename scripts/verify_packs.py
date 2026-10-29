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
  - the release is the one worldwide pack ("world"), it has at least
    --min-products products, and at most 1% of them lack nutrition (only the
    must-keep products build_packs.py adds whatever their nutrition);
  - against the previous release, which is required unless --first-release is
    given, it hasn't shrunk by more than 5%: it must hold at least 95% as many
    products as any previous pack had with nutrition (the worldwide pack, or
    during the change from country packs, the biggest country).

Popular products are checked separately, by popular_gate.py.

Usage:
  verify_packs.py --dist dist (--previous index.json | --first-release) --min-products N
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

SCHEMA_VERSION = 3
SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
HEX64 = re.compile(r"[0-9a-f]{64}")
MAX_ASSET = 2 * 1024**3 - 1
MAX_UNPACKED = 2_000_000_000  # the worldwide pack was 620 MB in October 2026; a bigger claim is a red flag
MAX_SHRINK = 0.05
# Only must-keep products (the watch-list and the most-scanned) may lack nutrition: about 10,000 at most.
MAX_WITHOUT_NUTRITION = 0.01
PACK = "world"

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
    # Types and missing values, so a loosened definition can't slip blanks through.
    "missing or mistyped required fields": (
        "barcode IS NULL OR name IS NULL OR has_nutrition IS NULL OR per_ml IS NULL OR scans IS NULL "
        "OR typeof(barcode) != 'text' OR typeof(name) != 'text' OR typeof(has_nutrition) != 'integer' "
        "OR typeof(per_ml) != 'integer' OR typeof(scans) != 'integer' OR id IS NOT rowid"
    ),
}


def normalise_sql(sql: str) -> str | None:
    """A table's CREATE statement without comments or layout, for comparing exactly. Anything but plain ASCII
    is refused (None): SQLite reads a non-breaking space as part of a name, so splitting on it would hide a change."""
    if not sql.isascii():
        return None
    no_comments = re.sub(r"--[^\n]*", "", sql)
    spaced = no_comments.replace("(", " ( ").replace(")", " ) ").replace(",", " , ")
    return " ".join(t for t in re.split(r"[ \t\r\n]+", spaced) if t).lower()


_EXPECTED_SQL: dict[str, str] | None = None


def expected_sql() -> dict[str, str]:
    """Every table's CREATE statement, from build_schema.EXPECTED_SQL (the app holds the same list). This runner's
    SQLite must write the schema exactly that way too, or the build stops here rather than on the phones."""
    global _EXPECTED_SQL
    if _EXPECTED_SQL is None:
        import build_schema

        con = sqlite3.connect(":memory:")
        con.executescript(build_schema.SCHEMA)
        made = {n: normalise_sql(s) for n, s in con.execute("SELECT name, sql FROM sqlite_schema") if s}
        con.close()
        if made != build_schema.EXPECTED_SQL:
            sys.exit("This SQLite writes the schema differently from build_schema.EXPECTED_SQL (and the app). Not publishing.")
        _EXPECTED_SQL = build_schema.EXPECTED_SQL
    return _EXPECTED_SQL


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
        tail = d.flush()
        written += len(tail)
        if written > expected:
            return False
        fout.write(tail)
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
            rows = con.execute("SELECT type, name, sql FROM sqlite_schema").fetchall()
            objects = {(t, n) for t, n, _ in rows}
            if objects != EXPECTED_OBJECTS:
                return [f"{c} has unexpected database objects: {sorted(objects ^ EXPECTED_OBJECTS)}"]
            sql = {n: normalise_sql(s) for _, n, s in rows if s}
            if sql != expected_sql():
                return [f"{c} has tables that aren't defined exactly as the build defines them"]
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
            if con.execute("SELECT count(*) FROM meta WHERE key IS NULL OR value IS NULL").fetchone()[0]:
                problems.append(f"{c} has empty build details")
            meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
            if meta.get("country") != c:
                problems.append(f"{c} meta says it's {meta.get('country')!r}")
            return problems
        finally:
            con.close()


def gates(index: dict, previous: dict | None, args) -> list[str]:
    problems = []
    packs = {p["country"]: p for p in index["packs"]}
    if set(packs) != {PACK}:
        return [f"the release should be the one {PACK} pack, not {sorted(packs)}"]
    count = packs[PACK]["product_count"]
    print(f"{PACK}: {count} products")
    if count < args.min_products:
        problems.append(f"{PACK} has only {count} products (minimum {args.min_products})")
    without = count - packs[PACK]["products_with_nutrition"]
    if without > count * MAX_WITHOUT_NUTRITION:
        problems.append(f"{PACK}: {without} products have no nutrition (only must-keep products may, at most {MAX_WITHOUT_NUTRITION:.0%})")
    if previous:
        for p in previous.get("packs", []):
            before = p.get("products_with_nutrition") if isinstance(p, dict) else None
            if type(before) is int and count < before * (1 - MAX_SHRINK):
                problems.append(f"{PACK} has {count} products, fewer than {1 - MAX_SHRINK:.0%} of the {before} with nutrition in last week's {p.get('country')} pack")
    return problems


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", required=True, type=Path)
    prev = ap.add_mutually_exclusive_group(required=True)
    prev.add_argument("--previous", type=Path, help="the last published index.json, already signature-checked")
    prev.add_argument("--first-release", action="store_true", help="there is no previous release to compare with")
    ap.add_argument("--min-products", type=int, required=True)
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
    problems += gates(index, previous, args)
    if problems:
        sys.exit("Not publishing:\n  " + "\n  ".join(problems))
    print(f"verified {len(index['packs'])} packs")


if __name__ == "__main__":
    main()
