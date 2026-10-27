# UK food pack

A weekly, offline-friendly database of UK food products, built from
[Open Food Facts](https://world.openfoodfacts.org) for barcode lookups and
food search on the phone with no signal.

## What it does

Every Monday a GitHub Actions job:

1. Downloads Open Food Facts' nightly products export.
2. Keeps every product sold in the UK that has a valid barcode and a name, so
   a scan can at least recognise it. Nutrition is included where it's
   plausible; anything impossible (over 900 kcal per 100 g, negative values,
   macros adding up to more than 100 g) is blanked, not guessed. Energy given
   only in kJ is converted to kcal.
3. Writes a SQLite database with a full-text search index on name and brand.
4. Compresses it (raw DEFLATE), signs it with Ed25519 and publishes it as a
   release alongside `manifest.json`. The last four releases are kept.

## Using a pack

Always-current URLs:

- Manifest: `https://github.com/soss-lesig/uk-food-pack/releases/latest/download/manifest.json`
- Pack: `https://github.com/soss-lesig/uk-food-pack/releases/latest/download/uk-food-pack.sqlite.deflate`

To accept a pack, check that:

- `schema_version` is one you understand and `version` is newer than the pack you have
- the file's SHA-256 matches `sha256`
- the Ed25519 `signature` (base64, over the compressed file's bytes) verifies
  against `keys/food-pack-public.pem`

Then inflate it (raw DEFLATE, no zlib header) and open it read-only.

### Schema (version 1)

- `products`: `barcode` (unique), `name`, `brand`, `quantity`, `serving_size`,
  `serving_g`, `has_nutrition` (1 or 0), and per 100 g `kcal`, `protein`,
  `carbs`, `sugar`, `fat`, `fibre`, `salt` (all grams except kcal; all null
  when `has_nutrition` is 0), plus `updated_at` (Unix time)
- `products_fts`: FTS5 index on `name` and `brand`, accent-insensitive, with
  prefix indexes for search-as-you-type
- `meta`: key/value build details, including `pack_version` and `product_count`

## Building locally

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/build_pack.py --input products.csv.gz --out dist
```

Leave out `--sign-key` and the manifest's `signature` is null.

## Licences

The data is under the ODbL 1.0, see [DATA-LICENCE.md](DATA-LICENCE.md).
Contains data from Open Food Facts, available under the Open Database License.
The code is MIT, see [LICENSE](LICENSE).

The signing key lives only in the repo's Actions secrets. If it's ever lost or
leaked, generate a new pair, replace the secret and the public key here, and
ship an app update with the new public key.
