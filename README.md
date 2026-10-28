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

Four jobs, so the signing key never shares a room with anything downloaded or
anything that can write to the repo:

1. **build** (no secrets, read-only): streams the dump and hashes it as it
   reads, never storing it (`scripts/extract_dump.py`); any hash mismatch stops
   the build. Then writes one SQLite pack per country with at least 500
   products (`scripts/build_packs.py`). Products sold in several countries go
   in each. Country names must be plain slugs (`a-z`, `0-9`, hyphens), since
   they become file names; accents are stripped and Türkiye is merged into
   turkey. Nutrition is only kept when it's plausible: over 900 kcal per 100 g,
   negative values, macros over 105 g, more sugar than carbs, kcal and kJ that
   disagree (outside 3 to 5.5 kJ per kcal), or energy under half or over three
   times what the protein, carbs, fat and alcohol add up to blank it rather than
   guess. Each product's nutrition comes from one label, never a mix. Drinks
   (per 100 ml) are marked so the app shows ml. Tags that aren't countries today
   (world, yugoslavia and the like) get no pack.
2. **verify** (no secrets, read-only, `scripts/verify_packs.py`): trusts
   nothing the build says about itself. Checks the index's exact shape, that
   the folder holds only the listed files, every hash and size, then opens
   every pack: exactly the expected tables (no views or triggers), SQLite's
   integrity check, every value in range, and the counts recounted. Gates: the
   UK pack has at least 150,000 products and 60% with nutrition; at least 80%
   of Open Food Facts' live UK count when their API answers (a warning on the
   run when it doesn't); against last week's signature-checked release, no
   country over 5,000 products shrinks by more than 20% and the UK's share with
   nutrition doesn't fall by more than 10 points.
3. **sign** (the key and nothing else, `scripts/sign_packs.py`): no network
   calls and no write access. Re-checks the shape and every hash, so it signs
   exactly what was verified, then signs each pack and the index with Ed25519.
   It refuses any index that isn't byte for byte the one verify passed (verify
   hands on its SHA-256, and the index pins every pack's), so nothing swapped
   in between can be signed. The key is written to a file only for this step and
   deleted however it ends.
4. **publish** (write access, no key, `scripts/stage_release.py`): proves every
   signature against `keys/food-pack-public.pem`, copies only the index, its
   signature and the listed packs into a release folder, and publishes that.

Dependencies are pinned by hash in `requirements.txt`; actions are pinned to
commits. `tests/` covers the scripts and runs at the start of every build.

## Setting up the key

The signing key lives in a GitHub **Environment** called `food-pack-signing`,
not in the repo's general secrets, so only workflow runs from `main` can use it.

**Do all of this in Settings before merging the workflow.** If the workflow names
an environment that doesn't exist yet, GitHub creates it with no branch rule, and
the job quietly falls back to a repository-level secret of the same name.

1. Settings, Environments, New environment: `food-pack-signing`.
2. Deployment branches and tags: Selected branches, add `main`.
3. Add the environment secret `FOOD_PACK_SIGNING_KEY`. GitHub never shows a
   secret again once saved, so:
   - **If you still have the private key file** (offline), paste that.
   - **If not**, make a new pair and follow "Changing the key" below.
4. Delete any repository-level secret with that name.
5. Settings, Branches: protect `main` (no force pushes, no deletion).
6. Keep an offline backup of the private key (a password manager is fine).

### Changing the key

1. `openssl genpkey -algorithm ed25519 -out new-key.pem` and
   `openssl pkey -in new-key.pem -pubout -out keys/food-pack-public.pem`
   (copy the old public key to `keys/previous-public.pem` first).
2. Replace the environment secret with `new-key.pem`, back it up, delete the file.
3. Update the public key built into the app and ship an update. Old app versions
   refuse new packs until they update, which is the point.
4. The verify job checks last week's index against the current key, or
   `keys/previous-public.pem` during the change, so the shrink gates keep working.
   Delete `keys/previous-public.pem` once a release signed with the new key is out.
   If nothing signed with the old key was ever published, skip the previous key.

## Using the packs

- Index: `https://github.com/soss-lesig/world-food-packs/releases/latest/download/index.json`
- Its signature: `.../index.json.sig` (base64 Ed25519 over the exact bytes of index.json)
- A pack, from the index's own release so a mid-download publish can't mix
  weeks: `https://github.com/soss-lesig/world-food-packs/releases/download/packs-<version>/<country>.sqlite.deflate`

To accept the index, verify `index.json.sig` against `keys/food-pack-public.pem`,
and refuse a `version` older than the newest one already seen. To accept a pack,
check its size, SHA-256 and `signature` from the index entry, then inflate it
(raw DEFLATE, no zlib header) and open it read-only.

### Schema (version 3)

- `products`: `barcode` (unique), `name`, `brand`, `quantity`, `serving_size`,
  `serving_g`, `has_nutrition` (1 or 0), per 100 g (or 100 ml when `per_ml`
  is 1) `kcal`, `protein`, `carbs`, `sugar`, `fat`, `fibre`, `salt` (grams
  except kcal; null when `has_nutrition` is 0), `updated_at` (Unix time),
  `scans` (unique scans on Open Food Facts, for putting popular products
  first) and `per_ml`
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
To check and sign locally, with a throwaway key (`openssl genpkey -algorithm ed25519`):

```sh
.venv/bin/python -I scripts/verify_packs.py --dist dist --first-release --min-uk 150000
.venv/bin/python -I scripts/sign_packs.py --dist dist --key test-key.pem --out signed
.venv/bin/python -m unittest discover -s tests -v
```

## Licences

The data is under the ODbL 1.0, see [DATA-LICENCE.md](DATA-LICENCE.md).
Contains data from Open Food Facts, available under the Open Database License.
The code is MIT, see [LICENSE](LICENSE).

