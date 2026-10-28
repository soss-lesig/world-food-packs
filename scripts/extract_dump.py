#!/usr/bin/env python3
"""Stream the Open Food Facts MongoDB dump into a slim file of just what the packs need.

Why the dump: the CSV, JSONL and Parquet exports have hardly any products created
since 2022 (checked October 2026), while the raw database dump has them all.

The dump is a gzipped `mongodump --archive` (about 16 GB). This reads it as a
stream, from a file or stdin, hashing the compressed bytes as it goes, and never
stores it. Only the off.products collection is read. Each product becomes one
line of JSON with a dozen fields.

If --sha256 is given and the hash of what was read doesn't match, the output is
deleted and the script fails: nothing gets built from a file we can't vouch for.

Usage:
  curl -sL .../openfoodfacts-mongodbdump.gz | extract_dump.py - --sha256 HEX --out slim.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path

from bson.raw_bson import RawBSONDocument

MAGIC = b"\x6d\xe2\x99\x81"
TERMINATOR = 0xFFFFFFFF
NUTRIENTS = {
    "kcal": "energy-kcal_100g",
    "kj": "energy-kj_100g",
    "protein": "proteins_100g",
    "carbs": "carbohydrates_100g",
    "sugar": "sugars_100g",
    "fat": "fat_100g",
    "fibre": "fiber_100g",
    "salt": "salt_100g",
}


class HashingGunzip:
    """Reads gzip from a binary stream, hashing the compressed bytes and giving back plain bytes."""

    def __init__(self, raw):
        self.raw = raw
        self.hash = hashlib.sha256()
        self.inflate = zlib.decompressobj(wbits=31)
        self.buffer = bytearray()
        self.done = False

    def _fill(self, n: int) -> None:
        while len(self.buffer) < n and not self.done:
            chunk = self.raw.read(1 << 20)
            if not chunk:
                self.buffer += self.inflate.flush()
                self.done = True
                break
            self.hash.update(chunk)
            data = self.inflate.decompress(chunk)
            # Concatenated gzip members: start a new decompressor on what's left.
            while self.inflate.eof and self.inflate.unused_data:
                rest = self.inflate.unused_data
                self.inflate = zlib.decompressobj(wbits=31)
                data += self.inflate.decompress(rest)
            self.buffer += data

    def read(self, n: int) -> bytes:
        self._fill(n)
        out = bytes(self.buffer[:n])
        del self.buffer[:n]
        return out

    def drain(self) -> None:
        """Read to the end so the hash covers the whole file."""
        while not self.done:
            self._fill(len(self.buffer) + (1 << 24))
            self.buffer.clear()


def documents(stream: HashingGunzip):
    """Yield (namespace, raw BSON bytes) for every document in a mongodump archive."""
    if stream.read(4) != MAGIC:
        sys.exit("not a mongodump archive")
    in_metadata = True
    namespace = None
    expect_header = False

    def next_doc():
        head = stream.read(4)
        if len(head) < 4:
            return None
        (length,) = struct.unpack("<I", head)
        if length == TERMINATOR:
            return b""
        return head + stream.read(length - 4)

    prelude = next_doc()
    if not prelude:
        sys.exit("archive has no prelude")
    while True:
        raw = next_doc()
        if raw is None:
            return
        if raw == b"":
            if in_metadata:
                in_metadata = False
            namespace = None
            expect_header = True
            continue
        if in_metadata:
            continue
        if expect_header or namespace is None:
            header = RawBSONDocument(raw)
            if header.get("EOF"):
                namespace = None
            else:
                namespace = f"{header.get('db')}.{header.get('collection')}"
            expect_header = False
            continue
        yield namespace, raw


def number(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) < 1e7 else None


def whole(v) -> int:
    """Counts and timestamps: whole numbers, 0 when missing."""
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def name_of(doc) -> str | None:
    lang = doc.get("lang") or ""
    candidates = [doc.get("product_name_en"), doc.get("product_name"), doc.get(f"product_name_{lang}") if lang else None]
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()
    for key in doc.keys():
        if key.startswith("product_name_"):
            c = doc.get(key)
            if isinstance(c, str) and c.strip():
                return c.strip()
    return None


# The 2026 nutrition structure: nutrition.aggregated_set.nutrients[name] = {value, unit, source}.
NEW_NAMES = {
    "kcal": "energy-kcal",
    "kj": "energy-kj",
    "protein": "proteins",
    "carbs": "carbohydrates",
    "sugar": "sugars",
    "fat": "fat",
    "fibre": "fiber",
    "salt": "salt",
}
TO_GRAMS = {"g": 1.0, "mg": 0.001, "µg": 0.000001, "mcg": 0.000001, "kcal": 1.0, "kj": 1.0}


def aggregated(doc) -> dict:
    """Per-100 g (or ml) values from nutrition.aggregated_set, as sold, ignoring estimates from ingredients."""
    nutrition = doc.get("nutrition")
    if not hasattr(nutrition, "get"):
        return {}
    agg = nutrition.get("aggregated_set")
    if not hasattr(agg, "get") or agg.get("per") not in ("100g", "100ml") or agg.get("preparation", "as_sold") != "as_sold":
        return {}
    nutrients = agg.get("nutrients")
    if not hasattr(nutrients, "get"):
        return {}
    out = {}
    for field, name in NEW_NAMES.items():
        n = nutrients.get(name)
        if not hasattr(n, "get") or n.get("source") == "estimate":
            continue
        value = number(n.get("value"))
        factor = TO_GRAMS.get(str(n.get("unit") or "g").lower())
        if value is not None and factor is not None:
            out[field] = value * factor
    if "salt" not in out:
        sodium = nutrients.get("sodium")
        if hasattr(sodium, "get") and sodium.get("source") != "estimate":
            value = number(sodium.get("value"))
            factor = TO_GRAMS.get(str(sodium.get("unit") or "g").lower())
            if value is not None and factor is not None:
                out["salt"] = value * factor * 2.5
    return out


def slim(raw: bytes) -> dict | None:
    doc = RawBSONDocument(raw)
    code = doc.get("code")
    if not isinstance(code, str):
        return None
    countries = doc.get("countries_tags")
    if not isinstance(countries, list) or not countries:
        return None
    nut = doc.get("nutriments") or {}
    out = {
        "code": code.strip(),
        "name": name_of(doc),
        "brands": doc.get("brands") if isinstance(doc.get("brands"), str) else None,
        "quantity": doc.get("quantity") if isinstance(doc.get("quantity"), str) else None,
        "serving_size": doc.get("serving_size") if isinstance(doc.get("serving_size"), str) else None,
        "serving_quantity": number(doc.get("serving_quantity")),
        "countries": [c for c in countries if isinstance(c, str)],
        "scans": whole(doc.get("unique_scans_n")) or whole(doc.get("scans_n")),
        "updated_at": whole(doc.get("last_modified_t")),
        "obsolete": bool(doc.get("obsolete")),
    }
    new = aggregated(doc)
    for field, key in NUTRIENTS.items():
        old = number(nut.get(key)) if hasattr(nut, "get") else None
        out[field] = new.get(field, old)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="path to the .gz dump, or - for stdin")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--sha256", help="expected SHA-256 of the compressed dump")
    args = ap.parse_args()

    raw = sys.stdin.buffer if args.input == "-" else open(args.input, "rb")
    stream = HashingGunzip(raw)
    kept = seen = 0
    with args.out.open("w", encoding="utf-8") as out:
        for namespace, doc in documents(stream):
            if namespace != "off.products":
                continue
            seen += 1
            row = slim(doc)
            if row and row["name"]:
                out.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                kept += 1
            if seen % 500_000 == 0:
                print(f"{seen} products read, {kept} kept", file=sys.stderr)
    stream.drain()
    digest = stream.hash.hexdigest()
    print(f"{seen} products read, {kept} kept, sha256 {digest}")
    if args.sha256 and digest != args.sha256.lower():
        args.out.unlink(missing_ok=True)
        sys.exit(f"dump hash mismatch: expected {args.sha256}, got {digest}")


if __name__ == "__main__":
    main()
