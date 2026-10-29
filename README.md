# World food packs

A weekly, offline-friendly food database: one worldwide pack of every product
with usable nutrition (plus the popular products that must never go), built from
[Open Food Facts](https://world.openfoodfacts.org) for barcode lookups and
food search on the phone with no signal.

## Why one worldwide pack

Until October 2026 this built one pack per country. Open Food Facts' dump of
28 October had wrong `countries_tags` on popular products: about 439 of them
(Red Bull Sugarfree, Coca-Cola, Nutella and others) dropped out of the UK pack
and about 5,600 lost their nutrition, while every count-based gate passed. Open
Food Facts fixed the data the same day, but the packs had already gone out.
So country tags are now ignored entirely, and a safety check on popular
products stands between the build and a release.

What's in it:

- every product with a barcode, a name and **usable nutrition**, meaning energy
  plus protein, carbohydrate and fat per 100 g (or 100 ml), all passing the
  plausibility checks below;
- whatever their nutrition, the **must-keep** products: the watch-list
  (`popular-watchlist.txt`), the 5,000 most-scanned products in this week's
  dump and the 5,000 most-scanned in last week's pack;
- every product that had nutrition in last week's pack.

**Nutrition is never lost.** When this week's dump has no usable nutrition for
a product that last week's pack had, last week's values are carried forward. A
must-keep product that has gone from the dump altogether comes back as last
week's pack had it. Last week's pack is downloaded from its release and checked
against last week's signed index before the build reads it.

Products with no nutrition at all, and not popular, are left out; the app looks
those up online when they're scanned. Measured from the dump of 29 October
2026: see "Sizes" below.

## Where the data comes from

Only from Open Food Facts' raw database dump
(`openfoodfacts-mongodbdump.gz`, about 16 GB), checked against the SHA-256 they
publish alongside it. Nothing else is read.

Why not their CSV, JSONL or Parquet exports: in October 2026 all three held
about 2.1 million products with almost nothing created since 2022, while the
database dump held 4.8 million (see openfoodfacts-server issue #15024 for one
cause).

Since 2026 Open Food Facts stores nutrition in `nutrition.aggregated_set`
rather than `nutriments`; the build reads both, preferring the new one, and
ignores values marked as estimates from ingredients.

## What the weekly build does

Four jobs, so the signing key never shares a room with anything downloaded or
anything that can write to the repo:

1. **build** (no secrets, read-only): streams the dump and hashes it as it
   reads, never storing it (`scripts/extract_dump.py`); any hash mismatch stops
   the build. Then writes the one SQLite pack, `world.sqlite.deflate`
   (`scripts/build_packs.py`), with what's described above. Country tags aren't
   read at all. Nutrition is only kept when it's plausible: over 900 kcal per
   100 g, negative values, macros over 105 g, more sugar than carbs, kcal and
   kJ that disagree (outside 3 to 5.5 kJ per kcal), or energy under half or
   over three times what the protein, carbs, fat and alcohol add up to blank it
   rather than guess (and a product with blanked nutrition only gets in if it's
   must-keep). Where a barcode appears twice, the most recently
   edited record decides. Each product's nutrition comes from one label, never
   a mix. Drinks (per 100 ml) are marked so the app shows ml.
2. **verify** (no secrets, read-only, `scripts/verify_packs.py`): trusts
   nothing the build says about itself. Checks the index's exact shape, that
   the folder holds only the listed files, every hash and size, then opens
   the pack: exactly the expected tables (no views or triggers), SQLite's
   integrity check, every value in range, and the counts recounted. Gates: the
   release is the one `world` pack with at least 2,500,000 products
   (`MIN_PRODUCTS`), at most 1% of them without nutrition, and against
   last week's signature-checked release it holds at least 95% as many products
   as last week's pack had with nutrition (during the change from country
   packs, the biggest country's). Then the safety check below.
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

## The safety check

`scripts/popular_gate.py`, in the verify job, refuses the release when popular
products vanish or lose their nutrition. Totals can't see this: on 28 October
the UK pack barely shrank while Coca-Cola and Nutella vanished.

- **Watch-list** (`popular-watchlist.txt`): fixed barcodes that must always be
  in the pack with nutrition, starting with Red Bull Sugarfree (90162800) and
  the best-known products the bad dump dropped. Any one missing stops the
  release. Add a line (`barcode  # name`) to watch another, but only a product
  that has nutrition on Open Food Facts.
- **Last week's most-scanned:** the 5,000 most-scanned products with nutrition
  in last week's pack (the worldwide one; for the first worldwide release, the
  last UK pack). If any of them is missing or has lost its nutrition, the
  release stops. The build carries these forward, so a gap means the build
  itself went wrong.

Measured on real packs from October 2026:

| Comparison (top 5,000 most-scanned) | Gone or lost nutrition |
| --- | --- |
| UK pack, 27 October dump to the bad 28 October dump | 355 (7.1%): refused |
| UK pack of 28 October to a worldwide pack of the 29 October dump, usable nutrition only, nothing carried forward | 29 (Nutella glass, Baileys and others whose fat or protein had gone) |
| The same, as built now (must-keep and carried forward) | 0 |
| Worldwide pack of an earlier dump to the 29 October one | 0 |

Of the UK pack's 144,321 products with nutrition, all 144,321 are in the
worldwide pack with nutrition (before carrying forward, 1,803 weren't).

If a genuine change ever trips the gate, the run's log lists the products;
check them on Open Food Facts, then either wait for their fix or run with
`--max-gone` raised for that one release.

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
3. Run the workflow by hand and wait for a release signed with the new key
   **before** installing an app with the new public key: that app deletes packs
   signed with any other key, and an old-key index won't verify, so until a
   new-key release exists it would have no food data.
4. Then update the public key built into the app and ship the update. Old app
   versions refuse new packs until they update, which is the point.
5. The verify job checks last week's index against the current key, or
   `keys/previous-public.pem` during the change, so the shrink gates keep working.
   Delete `keys/previous-public.pem` once a release signed with the new key is out.
   If nothing signed with the old key was ever published, skip the previous key.

## Using the pack

- Index: `https://github.com/soss-lesig/world-food-packs/releases/latest/download/index.json`
- Its signature: `.../index.json.sig` (base64 Ed25519 over the exact bytes of index.json)
- The pack, from the index's own release so a mid-download publish can't mix
  weeks: `https://github.com/soss-lesig/world-food-packs/releases/download/packs-<version>/world.sqlite.deflate`

The index keeps the shape it had for country packs, so older app builds still
read it: `packs` simply has one entry, with `country` set to `world`. There are
no diff updates; the app downloads the whole pack when its SHA-256 changes.

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
- `meta`: build details, including `pack_version`, `country` (always `world`),
  `product_count` and `source_sha256`

`has_nutrition` is 0 only for the few must-keep products with no usable
energy (399 in the 29 October build, Red Bull Energy Drink among them); a scan
of one still finds its name, and the app looks it up online for nutrition.

### Sizes

From the dump of 29 October 2026 (SHA-256 `968023f3...`), carried forward from
the UK pack of 28 October:

| | |
| --- | --- |
| Products | 3,187,780 |
| With nutrition | 3,187,381 (99.99%) |
| With energy, protein, carbs and fat | 3,185,526 (99.93%) |
| Must-keep products without usable nutrition | 2,254 |
| Download (raw DEFLATE) | 241.9 MB |
| On the phone (SQLite with search index) | 623.2 MB |
| Build time, after the dump is read | about 2 minutes, 1.8 GB memory peak |

## Building locally

```sh
python3 -m venv .venv && .venv/bin/pip install duckdb==2.0.0 pymongo==4.10.1
SHA=$(curl -s https://static.openfoodfacts.org/data/gz-sha256sum | awk '$2=="openfoodfacts-mongodbdump.gz"{print $1}')
curl -sL https://static.openfoodfacts.org/data/openfoodfacts-mongodbdump.gz \
  | .venv/bin/python -I scripts/extract_dump.py - --sha256 "$SHA" --out data/slim.jsonl
.venv/bin/python -I scripts/build_packs.py --slim data/slim.jsonl --source-sha256 "$SHA" --out dist \
  --watchlist popular-watchlist.txt --first-release   # or --previous-index prev/index.json --previous-dir prev
```

About 20 minutes to read the dump and 2 to build, with a 2 GB memory peak.
To check and sign locally, with a throwaway key (`openssl genpkey -algorithm ed25519`):

```sh
.venv/bin/python -I scripts/verify_packs.py --dist dist --first-release --min-products 2500000
.venv/bin/python -I scripts/popular_gate.py --dist dist --watchlist popular-watchlist.txt --first-release
.venv/bin/python -I scripts/sign_packs.py --dist dist --key test-key.pem --out signed
.venv/bin/python -m unittest discover -s tests -v
```

## Licences

The data is under the ODbL 1.0, see [DATA-LICENCE.md](DATA-LICENCE.md).
Contains data from Open Food Facts, available under the Open Database License.
The code is MIT, see [LICENSE](LICENSE).

