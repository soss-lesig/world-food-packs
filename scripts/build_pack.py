#!/usr/bin/env python3
"""Build the UK food pack from an Open Food Facts CSV export.

Reads the nightly products CSV (tab separated, optionally gzipped), keeps UK
products with usable nutrition data, writes a read-only SQLite database with a
full-text search index, compresses it with raw DEFLATE (Apple's
COMPRESSION_ZLIB reads this directly) and, if given a key, signs it with
Ed25519 and writes manifest.json.

Usage:
  build_pack.py --input products.csv.gz --out dist [--sign-key key.pem]
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

import duckdb

SCHEMA_VERSION = 1
PACK_NAME = "uk-food-pack.sqlite.deflate"
UK_TAG = "en:united-kingdom"
SOURCE = "Open Food Facts (https://world.openfoodfacts.org)"
LICENCE = "ODbL 1.0 (https://opendatacommons.org/licenses/odbl/1-0/)"
MIN_PRODUCTS = 20_000  # absolute floor; the workflow also checks against last week

# Filtering and sanity checks run in DuckDB, straight off the compressed CSV.
# A product is kept if it has a valid barcode and a name, so a scan can at
# least recognise it. Nutrition is kept only when it's plausible: values that
# can't be real (over 900 kcal per 100 g, negatives, macros adding up to more
# than 100 g) blank the nutrition rather than drop the product or guess.
QUERY = r"""
WITH raw AS (
  SELECT
    trim(code)                                   AS barcode,
    trim(product_name)                           AS name,
    nullif(trim(split_part(brands, ',', 1)), '') AS brand,
    nullif(trim(quantity), '')                   AS quantity,
    nullif(trim(serving_size), '')               AS serving_size,
    try_cast(serving_quantity AS DOUBLE)         AS serving_g,
    coalesce(
      try_cast("energy-kcal_100g" AS DOUBLE),
      try_cast("energy-kj_100g" AS DOUBLE) / 4.184) AS kcal,
    try_cast(proteins_100g AS DOUBLE)            AS protein,
    try_cast(carbohydrates_100g AS DOUBLE)       AS carbs,
    try_cast(sugars_100g AS DOUBLE)              AS sugar,
    try_cast(fat_100g AS DOUBLE)                 AS fat,
    try_cast(fiber_100g AS DOUBLE)               AS fibre,
    try_cast(salt_100g AS DOUBLE)                AS salt,
    try_cast(last_modified_t AS BIGINT)          AS updated_at
  FROM read_csv(
    $input, delim = '\t', quote = '', header = true, all_varchar = true,
    ignore_errors = true, max_line_size = 10000000)
  WHERE countries_tags LIKE '%' || $uk || '%'
),
named AS (
  SELECT * FROM raw
  WHERE regexp_full_match(barcode, '[0-9]{8,14}')
    AND name IS NOT NULL AND length(name) BETWEEN 2 AND 200
),
checked AS (
  SELECT *,
    kcal IS NOT NULL AND kcal BETWEEN 0 AND 900
    AND coalesce(protein, 0) BETWEEN 0 AND 100
    AND coalesce(carbs, 0)   BETWEEN 0 AND 100
    AND coalesce(sugar, 0)   BETWEEN 0 AND 100
    AND coalesce(fat, 0)     BETWEEN 0 AND 100
    AND coalesce(fibre, 0)   BETWEEN 0 AND 100
    AND coalesce(salt, 0)    BETWEEN 0 AND 100
    AND coalesce(protein, 0) + coalesce(carbs, 0) + coalesce(fat, 0) <= 105
    AND (sugar IS NULL OR carbs IS NULL OR sugar <= carbs + 1) AS ok
  FROM named
),
latest AS (
  SELECT *, row_number() OVER (
    PARTITION BY barcode ORDER BY updated_at DESC NULLS LAST) AS rn
  FROM checked
)
SELECT barcode, name, brand, quantity, serving_size,
       CASE WHEN serving_g BETWEEN 0 AND 5000 THEN round(serving_g, 1) END,
       CASE WHEN ok THEN 1 ELSE 0 END,
       CASE WHEN ok THEN round(kcal, 1) END,
       CASE WHEN ok THEN round(protein, 2) END,
       CASE WHEN ok THEN round(carbs, 2) END,
       CASE WHEN ok THEN round(sugar, 2) END,
       CASE WHEN ok THEN round(fat, 2) END,
       CASE WHEN ok THEN round(fibre, 2) END,
       CASE WHEN ok THEN round(salt, 3) END,
       updated_at
FROM latest WHERE rn = 1
ORDER BY barcode
"""

SCHEMA = """
PRAGMA page_size = 4096;
PRAGMA journal_mode = DELETE;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE products (
  id           INTEGER PRIMARY KEY,
  barcode      TEXT NOT NULL UNIQUE,
  name         TEXT NOT NULL,
  brand        TEXT,
  quantity     TEXT,
  serving_size TEXT,
  serving_g    REAL,
  has_nutrition INTEGER NOT NULL,  -- 1 if the per-100 g values below are usable
  kcal         REAL,
  protein      REAL,
  carbs        REAL,
  sugar        REAL,
  fat          REAL,
  fibre        REAL,
  salt         REAL,
  updated_at   INTEGER
);
CREATE VIRTUAL TABLE products_fts USING fts5(
  name, brand,
  content = 'products', content_rowid = 'id',
  tokenize = 'unicode61 remove_diacritics 2',
  prefix = '2 3'
);
"""


def build_sqlite(input_path: str, db_path: Path, version: str) -> tuple[int, int]:
    rows = duckdb.execute(QUERY, {"input": input_path, "uk": UK_TAG}).fetchall()
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    con.executemany(
        "INSERT INTO products (barcode, name, brand, quantity, serving_size,"
        " serving_g, has_nutrition, kcal, protein, carbs, sugar, fat, fibre,"
        " salt, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    con.execute("INSERT INTO products_fts(products_fts) VALUES ('rebuild')")
    con.execute("INSERT INTO products_fts(products_fts) VALUES ('optimize')")
    meta = {
        "schema_version": str(SCHEMA_VERSION),
        "pack_version": version,
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "country": "GB",
        "product_count": str(len(rows)),
        "products_with_nutrition": str(sum(r[6] for r in rows)),
        "source": SOURCE,
        "licence": LICENCE,
    }
    con.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    con.commit()
    con.execute("VACUUM")
    ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    if ok != "ok":
        sys.exit(f"integrity check failed: {ok}")
    return len(rows), sum(r[6] for r in rows)


def deflate(src: Path, dst: Path) -> None:
    comp = zlib.compressobj(level=9, wbits=-15)  # raw DEFLATE, no header
    with src.open("rb") as fin, dst.open("wb") as fout:
        for chunk in iter(lambda: fin.read(1 << 20), b""):
            fout.write(comp.compress(chunk))
        fout.write(comp.flush())


def sign(path: Path, key: Path) -> str:
    with tempfile.NamedTemporaryFile() as sig:
        subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-inkey", str(key), "-rawin",
             "-in", str(path), "-out", sig.name],
            check=True,
        )
        return base64.b64encode(Path(sig.name).read_bytes()).decode()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--sign-key", type=Path)
    ap.add_argument("--min-products", type=int, default=MIN_PRODUCTS)
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    version = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    db = args.out / "uk-food-pack.sqlite"
    db.unlink(missing_ok=True)

    count, with_nutrition = build_sqlite(args.input, db, version)
    print(f"products: {count}, with nutrition: {with_nutrition}")
    if count < args.min_products:
        sys.exit(f"only {count} products, below {args.min_products}; not publishing")
    if with_nutrition < count // 2:
        sys.exit(f"only {with_nutrition} of {count} products have nutrition; not publishing")

    pack = args.out / PACK_NAME
    deflate(db, pack)
    data = pack.read_bytes()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "version": int(version),
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "country": "GB",
        "product_count": count,
        "products_with_nutrition": with_nutrition,
        "file": PACK_NAME,
        "compression": "deflate-raw",
        "size": len(data),
        "uncompressed_size": db.stat().st_size,
        "sha256": hashlib.sha256(data).hexdigest(),
        "signature": sign(pack, args.sign_key) if args.sign_key else None,
        "signature_algorithm": "ed25519",
        "source": SOURCE,
        "licence": LICENCE,
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({k: manifest[k] for k in ("version", "size", "uncompressed_size")}))


if __name__ == "__main__":
    main()
