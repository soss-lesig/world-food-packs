#!/usr/bin/env python3
"""Build per-country food packs from the slim file extract_dump.py makes.

The only source is Open Food Facts' raw database dump, read and hash-checked by
extract_dump.py. This keeps products with a usable barcode and name, and writes one read-only SQLite
pack per country with a full-text search index, compressed with raw DEFLATE
(Apple's COMPRESSION_ZLIB reads this directly). Products sold in several
countries go in each of those countries' packs.

This step has no secrets and is the only one that touches the downloaded data.
Signing happens afterwards in sign_packs.py, in a separate job.

Usage:
  build_packs.py --slim slim.jsonl --source-sha256 HEX --out dist
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sqlite3
import sys
import zlib
from pathlib import Path

import duckdb

SCHEMA_VERSION = 2
SOURCE = "Open Food Facts (https://world.openfoodfacts.org)"
LICENCE = "ODbL 1.0 (https://opendatacommons.org/licenses/odbl/1-0/)"
MIN_PER_COUNTRY = 500

# One row per product and country. Names prefer English, then the product's
# main language, then any name at all. Nutrition is per 100 g and only kept
# when it's plausible: impossible values blank the nutrition rather than drop
# the product, so a scan can still recognise it.
QUERY = r"""
WITH src AS (
  SELECT * FROM read_json($slim, format = 'newline_delimited', columns = {
    code: 'VARCHAR', name: 'VARCHAR', brands: 'VARCHAR', quantity: 'VARCHAR', serving_size: 'VARCHAR',
    serving_quantity: 'DOUBLE', countries: 'VARCHAR[]', scans: 'BIGINT', updated_at: 'BIGINT', obsolete: 'BOOLEAN',
    kcal: 'DOUBLE', kj: 'DOUBLE', protein: 'DOUBLE', carbs: 'DOUBLE', sugar: 'DOUBLE', fat: 'DOUBLE',
    fibre: 'DOUBLE', salt: 'DOUBLE'})
  WHERE NOT coalesce(obsolete, false)
    AND regexp_full_match(code, '[0-9]{8,14}')
    AND name IS NOT NULL AND length(name) BETWEEN 2 AND 200
),
nut AS (SELECT *, coalesce(kcal, kj / 4.184) AS energy FROM src),
checked AS (
  SELECT *,
    energy IS NOT NULL AND energy BETWEEN 0 AND 900
    AND coalesce(protein, 0) BETWEEN 0 AND 100
    AND coalesce(carbs, 0)   BETWEEN 0 AND 100
    AND coalesce(sugar, 0)   BETWEEN 0 AND 100
    AND coalesce(fat, 0)     BETWEEN 0 AND 100
    AND coalesce(fibre, 0)   BETWEEN 0 AND 100
    AND coalesce(salt, 0)    BETWEEN 0 AND 100
    AND coalesce(protein, 0) + coalesce(carbs, 0) + coalesce(fat, 0) <= 105
    AND (sugar IS NULL OR carbs IS NULL OR sugar <= carbs + 1) AS ok
  FROM nut
),
latest AS (
  SELECT *, row_number() OVER (PARTITION BY code ORDER BY updated_at DESC NULLS LAST) AS rn
  FROM checked
)
SELECT
  replace(c, 'en:', '') AS country,
  code, name,
  nullif(trim(split_part(brands, ',', 1)), '') AS brand,
  nullif(trim(quantity), '') AS quantity,
  nullif(trim(serving_size), '') AS serving_size,
  CASE WHEN serving_quantity BETWEEN 0 AND 5000 THEN round(serving_quantity, 1) END AS serving_g,
  CASE WHEN ok THEN 1 ELSE 0 END AS has_nutrition,
  CASE WHEN ok THEN round(energy, 1) END,
  CASE WHEN ok THEN round(protein, 2) END,
  CASE WHEN ok THEN round(carbs, 2) END,
  CASE WHEN ok THEN round(sugar, 2) END,
  CASE WHEN ok THEN round(fat, 2) END,
  CASE WHEN ok THEN round(fibre, 2) END,
  CASE WHEN ok THEN round(salt, 3) END,
  updated_at,
  scans
FROM latest, unnest(countries) AS t(c)
WHERE rn = 1 AND c LIKE 'en:%'
ORDER BY country, code
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
  kcal REAL, protein REAL, carbs REAL, sugar REAL, fat REAL, fibre REAL, salt REAL,
  updated_at   INTEGER,
  scans        INTEGER NOT NULL DEFAULT 0  -- unique scans on Open Food Facts: how popular it is
);
CREATE VIRTUAL TABLE products_fts USING fts5(
  name, brand,
  content = 'products', content_rowid = 'id',
  tokenize = 'unicode61 remove_diacritics 2',
  prefix = '2 3'
);
"""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def deflate(src: Path, dst: Path) -> None:
    comp = zlib.compressobj(level=9, wbits=-15)  # raw DEFLATE, no header
    with src.open("rb") as fin, dst.open("wb") as fout:
        for chunk in iter(lambda: fin.read(1 << 20), b""):
            fout.write(comp.compress(chunk))
        fout.write(comp.flush())


def country_name(slug: str) -> str:
    small = {"and", "of", "the", "da", "de", "del"}
    words = slug.replace("-", " ").split()
    return " ".join(w if (i and w in small) else w.capitalize() for i, w in enumerate(words))


def write_pack(rows: list[tuple], db_path: Path, meta: dict) -> int:
    db_path.unlink(missing_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    con.executemany(
        "INSERT INTO products (barcode, name, brand, quantity, serving_size, serving_g, has_nutrition,"
        " kcal, protein, carbs, sugar, fat, fibre, salt, updated_at, scans)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    con.execute("INSERT INTO products_fts(products_fts) VALUES ('rebuild')")
    con.execute("INSERT INTO products_fts(products_fts) VALUES ('optimize')")
    with_nutrition = sum(r[6] for r in rows)
    meta = {**meta, "product_count": str(len(rows)), "products_with_nutrition": str(with_nutrition)}
    con.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    con.commit()
    con.execute("VACUUM")
    ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    if ok != "ok":
        sys.exit(f"integrity check failed for {db_path.name}: {ok}")
    return with_nutrition


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slim", required=True, type=Path, help="output of extract_dump.py")
    ap.add_argument("--source-sha256", required=True, help="SHA-256 of the dump, as checked by extract_dump.py")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--min-per-country", type=int, default=MIN_PER_COUNTRY)
    args = ap.parse_args()

    version = int(dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d"))
    built_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    args.out.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute(f"CREATE TABLE packed AS {QUERY}", {"slim": str(args.slim)})
    counts = con.execute("SELECT country, count(*) FROM packed GROUP BY 1 ORDER BY 1").fetchall()

    packs = []
    for country, count in counts:
        if count < args.min_per_country:
            continue
        items = [r[1:] for r in con.execute("SELECT * FROM packed WHERE country = ? ORDER BY code", [country]).fetchall()]
        db = args.out / f"{country}.sqlite"
        meta = {
            "schema_version": str(SCHEMA_VERSION), "pack_version": str(version), "built_at": built_at,
            "country": country, "source": SOURCE, "licence": LICENCE, "source_sha256": args.source_sha256,
        }
        with_nutrition = write_pack(items, db, meta)
        pack = args.out / f"{country}.sqlite.deflate"
        deflate(db, pack)
        packs.append({
            "country": country,
            "name": country_name(country),
            "file": pack.name,
            "product_count": len(items),
            "products_with_nutrition": with_nutrition,
            "size": pack.stat().st_size,
            "uncompressed_size": db.stat().st_size,
            "sha256": sha256(pack),
        })
        db.unlink()
        print(f"{country}: {len(items)} products, {pack.stat().st_size / 1e6:.1f} MB")

    index = {
        "schema_version": SCHEMA_VERSION,
        "version": version,
        "built_at": built_at,
        "source": SOURCE,
        "licence": LICENCE,
        "source_file": "openfoodfacts-mongodbdump.gz",
        "source_sha256": args.source_sha256,
        "compression": "deflate-raw",
        "signature_algorithm": "ed25519",
        "packs": packs,
    }
    (args.out / "index.unsigned.json").write_text(json.dumps(index, indent=2) + "\n")
    print(f"{len(packs)} country packs")


if __name__ == "__main__":
    main()
