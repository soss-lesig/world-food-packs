#!/usr/bin/env python3
"""Stream the Open Food Facts MongoDB dump into a slim file of just what the packs need.

Why the dump: the CSV, JSONL and Parquet exports have hardly any products created
since 2022 (checked October 2026), while the raw database dump has them all.

The dump is a gzipped `mongodump --archive` (about 16 GB). This reads it as a
stream, from a file or stdin, hashing the compressed bytes as it goes, and never
stores it. Only the off.products collection is read. Each product becomes one
line of JSON with a dozen fields.

Output goes to a temporary name and is only renamed to --out once the whole file
has been read and, if --sha256 is given, the hash matches. Otherwise the temporary
file is deleted and the script fails: nothing gets built from a file we can't vouch for.

Usage:
  curl -sL .../openfoodfacts-mongodbdump.gz | extract_dump.py - --sha256 HEX --out slim.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import unicodedata
import zlib
from pathlib import Path

from bson.raw_bson import RawBSONDocument

MAGIC = b"\x6d\xe2\x99\x81"
TERMINATOR = 0xFFFFFFFF
MAX_DOC = 16 * 1024 * 1024  # MongoDB's own document limit
OUT_CHUNK = 4 * 1024 * 1024  # most plain bytes one decompress call may produce
MAX_INT = 2**53
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
        self.pending = b""  # compressed bytes read but not yet inflated
        self.buffer = bytearray()
        self.done = False

    def _fill(self, n: int) -> None:
        while len(self.buffer) < n and not self.done:
            if self.pending:
                # max_length caps how much one call can produce, so a decompression bomb
                # can't exhaust memory; whatever it didn't get to waits in unconsumed_tail.
                self.buffer += self.inflate.decompress(self.pending, OUT_CHUNK)
                self.pending = self.inflate.unconsumed_tail
                if self.inflate.eof:
                    # Concatenated gzip members: start a new decompressor on what's left.
                    self.pending = self.inflate.unused_data + self.pending
                    self.inflate = zlib.decompressobj(wbits=31)
                continue
            chunk = self.raw.read(1 << 20)
            if not chunk:
                self.done = True
                break
            self.hash.update(chunk)
            self.pending = chunk

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
        if not 5 <= length <= MAX_DOC:
            sys.exit(f"bad document length {length}")
        body = stream.read(length - 4)
        if len(body) != length - 4:
            sys.exit("archive ends mid-document")
        return head + body

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
    """Counts and timestamps: whole numbers, 0 when missing or absurd."""
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        return 0
    return n if 0 <= n < MAX_INT else 0


def clean(v, limit: int) -> str | None:
    """Text for display: no control or text-direction characters, single spaces, capped length."""
    if not isinstance(v, str):
        return None
    kept = "".join(ch for ch in v if ch in "\t\n\r" or unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cs"))
    text = " ".join(kept.split())
    return text[:limit].strip() or None


def name_of(doc) -> str | None:
    lang = doc.get("lang") or ""
    candidates = [doc.get("product_name_en"), doc.get("product_name"), doc.get(f"product_name_{lang}") if lang else None]
    for c in candidates:
        if clean(c, 150):
            return clean(c, 150)
    for key in doc.keys():
        if key.startswith("product_name_") and clean(doc.get(key), 150):
            return clean(doc.get(key), 150)
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
MASS = {"g": 1.0, "mg": 0.001, "µg": 0.000001, "μg": 0.000001, "mcg": 0.000001}
UNITS = {"kcal": {"kcal": 1.0}, "kj": {"kj": 1.0}}  # energy in its own unit only; everything else is a mass


def factor_for(field: str, unit) -> float | None:
    """How to turn a value into grams (or kcal, or kJ). None if the unit makes no sense for the field."""
    allowed = UNITS.get(field, MASS)
    default = "" if field in UNITS else "g"
    return allowed.get(str(unit or default).lower())


def aggregated(doc) -> tuple[dict, str]:
    """Per-100 g or per-100 ml values from nutrition.aggregated_set, as sold, ignoring estimates.

    Returns the values and what they're per ("100g" or "100ml")."""
    nutrition = doc.get("nutrition")
    if not hasattr(nutrition, "get"):
        return {}, "100g"
    agg = nutrition.get("aggregated_set")
    if not hasattr(agg, "get") or agg.get("per") not in ("100g", "100ml") or agg.get("preparation", "as_sold") != "as_sold":
        return {}, "100g"
    nutrients = agg.get("nutrients")
    if not hasattr(nutrients, "get"):
        return {}, "100g"
    out = {}
    for field, name in NEW_NAMES.items():
        n = nutrients.get(name)
        if not hasattr(n, "get") or n.get("source") == "estimate":
            continue
        value = number(n.get("value"))
        factor = factor_for(field, n.get("unit"))
        if value is not None and factor is not None:
            out[field] = value * factor
    if "salt" not in out:
        sodium = nutrients.get("sodium")
        if hasattr(sodium, "get") and sodium.get("source") != "estimate":
            value = number(sodium.get("value"))
            factor = factor_for("sodium", sodium.get("unit"))
            if value is not None and factor is not None:
                out["salt"] = value * factor * 2.5
    return out, agg.get("per")


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
        "brands": clean(doc.get("brands"), 200),
        "quantity": clean(doc.get("quantity"), 60),
        "serving_size": clean(doc.get("serving_size"), 60),
        "serving_quantity": number(doc.get("serving_quantity")),
        "countries": [c for c in countries if isinstance(c, str)],
        "scans": whole(doc.get("unique_scans_n")) or whole(doc.get("scans_n")),
        "updated_at": whole(doc.get("last_modified_t")),
        "obsolete": bool(doc.get("obsolete")),
    }
    # One source per product: the 2026 structure if it has anything, otherwise the old
    # nutriments block. Never a mix of two different labels.
    new, per = aggregated(doc)
    if new:
        for field in NUTRIENTS:
            out[field] = new.get(field)
        out["per"] = per
    else:
        for field, key in NUTRIENTS.items():
            out[field] = number(nut.get(key)) if hasattr(nut, "get") else None
        out["per"] = "100ml" if doc.get("nutrition_data_per") == "100ml" else "100g"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="path to the .gz dump, or - for stdin")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--sha256", help="expected SHA-256 of the compressed dump")
    args = ap.parse_args()

    partial = args.out.with_name(args.out.name + ".partial")
    partial.unlink(missing_ok=True)
    args.out.unlink(missing_ok=True)
    try:
        extract(args.input, partial, args.sha256)
    except BaseException:
        partial.unlink(missing_ok=True)  # never leave a half-written, unverified file behind
        raise
    os.replace(partial, args.out)


def extract(source: str, partial: Path, expected: str | None) -> None:
    raw = sys.stdin.buffer if source == "-" else open(source, "rb")
    stream = HashingGunzip(raw)
    kept = seen = 0
    with partial.open("w", encoding="utf-8") as out:
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
    if expected and digest != expected.lower():
        sys.exit(f"dump hash mismatch: expected {expected}, got {digest}")


if __name__ == "__main__":
    main()
