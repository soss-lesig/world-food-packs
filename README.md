# World food packs

Weekly, offline-friendly food databases, one per country, built from
[Open Food Facts](https://world.openfoodfacts.org) for barcode lookups and
food search on the phone with no signal.

## Where the data comes from

Only from Open Food Facts' raw database dump
(`openfoodfacts-mongodbdump.gz`, about 16 GB), checked against the SHA-256 they
publish alongside it. Nothing else is read.

Why not their CSV, JSONL or Parquet exports: in October 2026 all three held
about 2.1 million products with almost nothing created since 2022, while the
database dump held 4.8 million (see openfoodfacts-server issue #15024 for one
cause). Building from the exports gave a UK pack of 65,871 products; the dump
gives 181,118.

Since 2026 Open Food Facts stores nutrition in `nutrition.aggregated_set`
rather than `nutriments`; the build reads both, preferring the new one, and
ignores values marked as estimates from ingredients.

## What the weekly build does

Two jobs, so the signing key never meets downloaded data:

1. **build** (no secrets): streams the dump and hashes it as it reads, never
   storing it (`scripts/extract_dump.py`). Any hash mismatch stops the build.
   Then writes one SQLite pack per country with at least 500 products
   (`scripts/build_packs.py`). Products sold in several countries go in each.
   Nutrition is only kept when it's plausible: over 900 kcal per 100 g,
   negative values, macros over 105 g or more sugar than carbs blank it rather
   than guess. Energy given only in kJ is converted.
2. **sign** (has the key, never reads the dump): re-hashes every pack, checks
   the gates, signs each pack and the index with Ed25519, proves every
   signature against `keys/food-pack-public.pem`, and publishes a release.
   Gates: the UK pack has at least 150,000 products and at least 80% of the
   live UK count when Open Food Facts' API answers; no country over 5,000
   products shrinks by more than 20% in a week; every file under 2 GiB.

Dependencies are pinned by hash in `requirements.txt`; actions are pinned to
commits.

## Using the packs

Always-current URLs:

- Index: `https://github.com/soss-lesig/world-food-packs/releases/latest/download/index.json`
- Its signature: `.../index.json.sig` (base64 Ed25519 over the exact bytes of index.json)
- A pack: `.../<country>.sqlite.deflate`, for example `united-kingdom.sqlite.deflate`

To accept the index, verify `index.json.sig` against `keys/food-pack-public.pem`.
To accept a pack, check its size, SHA-256 and `signature` from the index entry,
then inflate it (raw DEFLATE, no zlib header) and open it read-only.

### Schema (version 2)

- `products`: `barcode` (unique), `name`, `brand`, `quantity`, `serving_size`,
  `serving_g`, `has_nutrition` (1 or 0), per 100 g `kcal`, `protein`, `carbs`,
  `sugar`, `fat`, `fibre`, `salt` (grams except kcal; null when
  `has_nutrition` is 0), `updated_at` (Unix time) and `scans` (unique scans on
  Open Food Facts, for putting popular products first)
- `products_fts`: FTS5 index on `name` and `brand`, accent-insensitive, with
  prefix indexes for search-as-you-type
- `meta`: build details, including `pack_version`, `country`, `product_count`
  and `source_sha256`

## Building locally

```sh
python3 -m venv .venv && .venv/bin/pip install duckdb==2.0.0 pymongo==4.10.1
SHA=$(curl -s https://static.openfoodfacts.org/data/gz-sha256sum | awk '$2=="openfoodfacts-mongodbdump.gz"{print $1}')
curl -sL https://static.openfoodfacts.org/data/openfoodfacts-mongodbdump.gz \
  | .venv/bin/python -I scripts/extract_dump.py - --sha256 "$SHA" --out data/slim.jsonl
.venv/bin/python -I scripts/build_packs.py --slim data/slim.jsonl --source-sha256 "$SHA" --out dist
```

About 12 minutes to read the dump and 2 to build, with a 4 GB memory peak.
To try signing, use a throwaway key: `openssl genpkey -algorithm ed25519`.

## Licences

The data is under the ODbL 1.0, see [DATA-LICENCE.md](DATA-LICENCE.md).
Contains data from Open Food Facts, available under the Open Database License.
The code is MIT, see [LICENSE](LICENSE).

The signing key lives only in the repo's Actions secrets. If it's ever lost or
leaked, generate a new pair, replace the secret and the public key here, and
ship an app update with the new public key.
