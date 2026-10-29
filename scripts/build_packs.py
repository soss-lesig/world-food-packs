#!/usr/bin/env python3
"""Build the worldwide food pack from the slim file extract_dump.py makes.

The only source is Open Food Facts' raw database dump, read and hash-checked by
extract_dump.py. It writes one read-only SQLite pack with a full-text search
index, compressed with raw DEFLATE (Apple's COMPRESSION_ZLIB reads this directly).

Why one worldwide pack: the dump of 28 October 2026 had wrong country tags on
popular products, and about 439 of them (Red Bull Sugarfree, Coca-Cola, Nutella
and others) fell out of the UK pack. Country tags are now ignored entirely.

What goes in:
  - every product, from anywhere, with a usable barcode, a name and usable
    nutrition: energy plus protein, carbohydrate and fat per 100 g (or 100 ml),
    all of it passing the plausibility checks below;
  - and, whatever their nutrition, the products that must never be dropped: the
    watch-list (popular-watchlist.txt), the --top most-scanned products in this
    dump, and the --top most-scanned products of last week's pack.

Nutrition is never lost: every product that had nutrition in last week's pack
(its size and SHA-256 checked against last week's signed index) stays in, and
when last week's nutrition was usable and this dump's isn't, last week's values
are kept. A must-keep product that has vanished from the dump altogether is kept
as last week's pack had it.

This step has no secrets and is the only one that touches the downloaded data.
Signing happens afterwards in sign_packs.py, in a separate job.

Usage:
  build_packs.py --slim slim.jsonl --source-sha256 HEX --out dist --watchlist popular-watchlist.txt
                 (--previous-index previous/index.json --previous-dir previous | --first-release)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import itertools
import json
import sqlite3
import sys
import tempfile
import zlib
from collections.abc import Iterable
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_schema import SCHEMA  # noqa: E402  (shared with verify_packs.py)
import popular_gate  # noqa: E402  (the same watch-list, baseline and "most-scanned" the safety check uses)

SCHEMA_VERSION = 3
SOURCE = "Open Food Facts (https://world.openfoodfacts.org)"
LICENCE = "ODbL 1.0 (https://opendatacommons.org/licenses/odbl/1-0/)"
# The one pack. The index keeps its per-pack shape (country, file, counts, sizes, hash) so the app and older
# builds read it as before; "world" is simply the only entry.
PACK = "world"
PACK_NAME = "Worldwide"
BATCH = 50_000

# One row per barcode: the most recently edited record wins, before any nutrition rule, so an older duplicate
# can't stand in for the product as it is today. Names prefer English, then the product's main language, then any
# name at all. Nutrition is per 100 g (or 100 ml).
#
# Energy checks catch the classic data-entry slips: kJ typed into the kcal box
# (about 4.2 times too high) or the other way round. If both energies are given
# they must roughly agree (kJ is 4.184 x kcal; 3 to 5.5 allows for label
# rounding, which is common, while a ratio near 1 or 17 is a slip); the energy
# must be between half and three times what protein, carbs and fat add up to
# (plus alcohol at 5.53 kcal per % vol per 100 ml). Half catches kcal typed into
# the kJ box (a quarter of the real figure).
#
# Implausible values blank the nutrition (has_nutrition 0) rather than drop the product. USABLE is the rule for
# getting into the pack on its own merits: plausible, and energy, protein, carbs and fat all given. Must-keep
# products get in either way.
USABLE = "ok AND protein IS NOT NULL AND carbs IS NOT NULL AND fat IS NOT NULL"
QUERY = r"""
WITH src AS (
  SELECT * FROM read_json($slim, format = 'newline_delimited', columns = {
    code: 'VARCHAR', name: 'VARCHAR', brands: 'VARCHAR', quantity: 'VARCHAR', serving_size: 'VARCHAR',
    serving_quantity: 'DOUBLE', scans: 'BIGINT', updated_at: 'BIGINT', obsolete: 'BOOLEAN',
    kcal: 'DOUBLE', kj: 'DOUBLE', protein: 'DOUBLE', carbs: 'DOUBLE', sugar: 'DOUBLE', fat: 'DOUBLE',
    fibre: 'DOUBLE', salt: 'DOUBLE', alcohol: 'DOUBLE', per: 'VARCHAR'})
  WHERE NOT coalesce(obsolete, false)
    AND regexp_full_match(code, '[0-9]{8,14}')
    AND name IS NOT NULL AND length(name) BETWEEN 2 AND 200
),
nut AS (
  SELECT *, coalesce(kcal, kj / 4.184) AS energy,
    CASE WHEN protein IS NOT NULL AND carbs IS NOT NULL AND fat IS NOT NULL
         THEN 4 * protein + 4 * carbs + 9 * fat + 5.53 * coalesce(alcohol, 0) END AS from_macros
  FROM src
),
checked AS (
  SELECT *,
    energy IS NOT NULL AND energy BETWEEN 0 AND 900
    AND (kcal IS NULL OR kj IS NULL OR (kcal < 2 AND kj < 10) OR kj / nullif(kcal, 0) BETWEEN 3.0 AND 5.5)
    AND (from_macros IS NULL OR energy BETWEEN from_macros * 0.5 - 10 AND from_macros * 3 + 20)
    AND coalesce(protein, 0) BETWEEN 0 AND 100
    AND coalesce(carbs, 0)   BETWEEN 0 AND 100
    AND coalesce(sugar, 0)   BETWEEN 0 AND 100
    AND coalesce(fat, 0)     BETWEEN 0 AND 100
    AND coalesce(fibre, 0)   BETWEEN 0 AND 100
    AND coalesce(salt, 0)    BETWEEN 0 AND 100
    AND coalesce(alcohol, 0) BETWEEN 0 AND 100
    AND coalesce(protein, 0) + coalesce(carbs, 0) + coalesce(fat, 0) <= 105
    AND (sugar IS NULL OR carbs IS NULL OR sugar <= carbs + 1) AS ok
  FROM nut
),
latest AS (
  SELECT *, row_number() OVER (PARTITION BY code ORDER BY updated_at DESC NULLS LAST) AS rn
  FROM checked
)
SELECT
  code, left(name, 150) AS name,
  left(nullif(trim(split_part(brands, ',', 1)), ''), 100) AS brand,
  left(nullif(trim(quantity), ''), 60) AS quantity,
  left(nullif(trim(serving_size), ''), 60) AS serving_size,
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
  coalesce(scans, 0),
  CASE WHEN per = '100ml' THEN 1 ELSE 0 END AS per_ml,
  CASE WHEN """ + USABLE + """ THEN 1 ELSE 0 END AS usable
FROM latest
WHERE rn = 1
ORDER BY code
"""

COLUMNS = (
    "barcode, name, brand, quantity, serving_size, serving_g, has_nutrition,"
    " kcal, protein, carbs, sugar, fat, fibre, salt, updated_at, scans, per_ml"
)
NUTRITION = "has_nutrition, kcal, protein, carbs, sugar, fat, fibre, salt, per_ml"
PREVIOUS_USABLE = "has_nutrition = 1 AND kcal IS NOT NULL AND protein IS NOT NULL AND carbs IS NOT NULL AND fat IS NOT NULL"


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


def write_pack(rows: Iterable[tuple], db_path: Path, meta: dict) -> tuple[int, int]:
    """Write the rows (any iterable, read in batches so millions never sit in memory at once). Returns the product
    count and how many have nutrition."""
    db_path.unlink(missing_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript("PRAGMA page_size = 4096; PRAGMA journal_mode = DELETE;")
    con.executescript(SCHEMA)
    count = with_nutrition = 0
    it = iter(rows)
    while batch := list(itertools.islice(it, BATCH)):
        con.executemany(
            "INSERT INTO products (barcode, name, brand, quantity, serving_size, serving_g, has_nutrition,"
            " kcal, protein, carbs, sugar, fat, fibre, salt, updated_at, scans, per_ml)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            batch,
        )
        count += len(batch)
        with_nutrition += sum(r[6] for r in batch)
    con.execute("INSERT INTO products_fts(products_fts) VALUES ('rebuild')")
    con.execute("INSERT INTO products_fts(products_fts) VALUES ('optimize')")
    meta = {**meta, "product_count": str(count), "products_with_nutrition": str(with_nutrition)}
    con.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    con.commit()
    con.execute("VACUUM")
    ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    if ok != "ok":
        sys.exit(f"integrity check failed for {db_path.name}: {ok}")
    return count, with_nutrition


def rows_from(cursor) -> Iterable[tuple]:
    while batch := cursor.fetchmany(BATCH):
        yield from batch


def stage(rows: Iterable[tuple], con: sqlite3.Connection) -> None:
    """Every product of the dump, one per barcode, with QUERY's usable flag on the end. Nothing is dropped yet."""
    con.execute(
        "CREATE TABLE staging (barcode TEXT PRIMARY KEY, name TEXT, brand TEXT, quantity TEXT, serving_size TEXT,"
        " serving_g REAL, has_nutrition INTEGER, kcal REAL, protein REAL, carbs REAL, sugar REAL, fat REAL,"
        " fibre REAL, salt REAL, updated_at INTEGER, scans INTEGER, per_ml INTEGER, usable INTEGER)"
    )
    it = iter(rows)
    while batch := list(itertools.islice(it, BATCH)):
        con.executemany(f"INSERT INTO staging VALUES ({','.join('?' * 18)})", batch)
    con.commit()


def carry_forward(con: sqlite3.Connection, previous: Path, restore: Iterable[str]) -> tuple[int, int]:
    """Keep what last week's pack knew. Any product without usable nutrition in this dump takes last week's, if
    that was usable, or if this dump has none at all and last week had some; any barcode in `restore` that isn't
    in this dump at all comes back as last week's row. `previous` must already be checked against last week's
    signed index (popular_gate.open_pack). Returns how many products got their nutrition back and how many
    whole products came back."""
    con.execute("PRAGMA trusted_schema = OFF")
    con.execute("ATTACH DATABASE ? AS prev", [previous.resolve().as_uri() + "?mode=ro"])
    # Inside the subqueries, bare column names are last week's (p); staging's are qualified.
    filled = con.execute(
        f"UPDATE staging SET ({NUTRITION}, usable) ="
        f" (SELECT {NUTRITION}, ({PREVIOUS_USABLE}) FROM prev.products p WHERE p.barcode = staging.barcode)"
        f" WHERE usable = 0 AND EXISTS (SELECT 1 FROM prev.products p WHERE p.barcode = staging.barcode"
        f" AND (({PREVIOUS_USABLE}) OR (p.has_nutrition = 1 AND staging.has_nutrition = 0)))"
    ).rowcount
    con.execute("CREATE TEMP TABLE restore (barcode TEXT PRIMARY KEY)")
    con.executemany("INSERT OR IGNORE INTO restore VALUES (?)", [(c,) for c in restore])
    restored = con.execute(
        f"INSERT INTO staging ({COLUMNS}, usable) SELECT {COLUMNS}, ({PREVIOUS_USABLE}) FROM prev.products"
        " WHERE barcode IN restore AND barcode NOT IN (SELECT barcode FROM staging)"
    ).rowcount
    # Whatever had nutrition last week stays in, even when this week's is only partial.
    con.execute("CREATE TEMP TABLE IF NOT EXISTS must_keep (barcode TEXT PRIMARY KEY)")
    con.execute("INSERT OR IGNORE INTO must_keep SELECT barcode FROM prev.products WHERE has_nutrition = 1")
    con.commit()
    con.execute("DETACH DATABASE prev")
    return filled, restored


def kept(con: sqlite3.Connection, must_keep: Iterable[str], top: int) -> Iterable[tuple]:
    """The pack's rows: every product with usable nutrition, plus the must-keep barcodes and this dump's `top`
    most-scanned products whatever their nutrition, and (from carry_forward) everything last week's pack had
    with nutrition."""
    con.execute("CREATE TEMP TABLE IF NOT EXISTS must_keep (barcode TEXT PRIMARY KEY)")
    con.executemany("INSERT OR IGNORE INTO must_keep VALUES (?)", [(c,) for c in must_keep])
    con.execute("INSERT OR IGNORE INTO must_keep SELECT barcode FROM staging ORDER BY scans DESC, barcode LIMIT ?", [top])
    partial = con.execute(
        "SELECT count(*) FROM staging WHERE usable = 0 AND barcode IN must_keep"
    ).fetchone()[0]
    print(f"{partial} must-keep products have no usable nutrition and are kept anyway")
    return rows_from(con.execute(f"SELECT {COLUMNS} FROM staging WHERE usable = 1 OR barcode IN must_keep ORDER BY barcode"))


def previous_pack(args, tmp: Path) -> tuple[Path | None, list[str]]:
    """Last week's baseline pack, checked and unpacked into tmp, and its most-scanned products with nutrition."""
    if args.first_release:
        print("No previous release: nothing to carry forward")
        return None, []
    if not args.previous_dir:
        sys.exit("--previous-index needs --previous-dir")
    entry = popular_gate.pick_baseline(json.loads(args.previous_index.read_text()))
    if entry is None:
        sys.exit("the previous release has no pack to compare with")
    old = popular_gate.open_pack(entry, args.previous_dir, tmp)
    popular = [code for code, _, _ in popular_gate.popular(old, args.top)]
    old.close()
    return popular_gate.unpacked_path(entry, tmp), popular


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slim", required=True, type=Path, help="output of extract_dump.py")
    ap.add_argument("--source-sha256", required=True, help="SHA-256 of the dump, as checked by extract_dump.py")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--watchlist", required=True, type=Path, help="barcodes that must always be in the pack")
    prev = ap.add_mutually_exclusive_group(required=True)
    prev.add_argument("--previous-index", type=Path, help="the last published index.json, already signature-checked")
    prev.add_argument("--first-release", action="store_true", help="there is no previous release to carry forward from")
    ap.add_argument("--previous-dir", type=Path, help="where last week's baseline pack was downloaded")
    ap.add_argument("--top", type=int, default=popular_gate.TOP, help="how many most-scanned products must be kept")
    args = ap.parse_args()

    version = int(dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d"))
    built_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    args.out.mkdir(parents=True, exist_ok=True)
    watch = popular_gate.read_watchlist(args.watchlist)

    db = args.out / f"{PACK}.sqlite"
    meta = {
        "schema_version": str(SCHEMA_VERSION), "pack_version": str(version), "built_at": built_at,
        "country": PACK, "source": SOURCE, "licence": LICENCE, "source_sha256": args.source_sha256,
    }
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        staging = sqlite3.connect((tmp / "staging.sqlite").as_uri(), uri=True)
        duck = duckdb.connect()
        stage(rows_from(duck.execute(QUERY, {"slim": str(args.slim)})), staging)
        duck.close()
        previous, popular = previous_pack(args, tmp)
        if previous:
            filled, restored = carry_forward(staging, previous, popular + watch)
            print(f"carried forward from last week: nutrition for {filled} products, {restored} whole products")
        count, with_nutrition = write_pack(kept(staging, watch + popular, args.top), db, meta)
        staging.close()
    if count == 0:
        sys.exit("no products made it into the pack")
    pack = args.out / f"{PACK}.sqlite.deflate"
    deflate(db, pack)
    entry = {
        "country": PACK,
        "name": PACK_NAME,
        "file": pack.name,
        "product_count": count,
        "products_with_nutrition": with_nutrition,
        "size": pack.stat().st_size,
        "uncompressed_size": db.stat().st_size,
        "sha256": sha256(pack),
    }
    db.unlink()
    print(f"{PACK}: {count} products, {entry['uncompressed_size'] / 1e6:.1f} MB on the phone, {entry['size'] / 1e6:.1f} MB to download")

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
        "packs": [entry],
    }
    (args.out / "index.unsigned.json").write_text(json.dumps(index, indent=2) + "\n")


if __name__ == "__main__":
    main()
