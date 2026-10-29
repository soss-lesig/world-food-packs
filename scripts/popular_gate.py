#!/usr/bin/env python3
"""The safety check: refuse to publish a pack that has lost popular products or their nutrition.

Why: Open Food Facts' dump of 28 October 2026 had wrong data on popular products. About 439 of them (Red Bull
Sugarfree, Coca-Cola, Nutella and others) dropped out of the UK pack and about 5,600 lost their nutrition, and
every other check passed, because the totals barely moved. Counting products can't see that; this checks the
products people actually scan.

Two lists, both checked against the new pack:

  - the watch-list (popular-watchlist.txt): fixed barcodes that must always be in the pack, with nutrition.
    Any one missing stops the release.
  - the popular list: the --top most-scanned products with nutrition in the previous release's pack (the
    worldwide pack; until one exists, the UK pack of the last country release). If any of them is missing from
    the new pack or has lost its nutrition (more than --max-gone, which is 0), the release stops.

Why none may go: build_packs.py keeps the same lists whatever this week's dump says, carrying last week's row or
nutrition forward when the dump has lost them. So a gap here means the build itself went wrong. Measured on the
dump of 29 October 2026 against the UK pack of 28 October, without carrying forward, 29 of the top 5,000 would
have gone (Nutella glass, Baileys and others whose fat or protein had gone missing); with it, none.

The previous pack is only read after its size and SHA-256 match the previous index, whose signature the
workflow has already checked.

Usage:
  popular_gate.py --dist dist --watchlist popular-watchlist.txt
                  (--previous-index previous/index.json --previous-dir previous | --first-release)
  popular_gate.py --pick-baseline previous/index.json      prints the file to download from the last release
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_packs import MAX_UNPACKED, inflate, sha256  # noqa: E402

TOP = 5000
MAX_GONE = 0.0
SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
FILE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*\.sqlite\.deflate")
# Which pack of the previous release to compare with: the worldwide one, or during the change from country packs,
# the UK's (where the app's users are).
BASELINES = ("world", "united-kingdom")
BARCODE = re.compile(r"[0-9]{8,14}")


def read_watchlist(path: Path) -> list[str]:
    """Barcodes, one per line; anything after # is a note. A malformed line stops the build rather than being
    quietly skipped."""
    codes = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        code = line.split("#", 1)[0].strip()
        if not code:
            continue
        if not BARCODE.fullmatch(code):
            sys.exit(f"{path.name} line {n}: {code!r} isn't a barcode")
        codes.append(code)
    return codes


def pick_baseline(index: dict) -> dict | None:
    packs = {p.get("country"): p for p in index.get("packs", []) if isinstance(p, dict)}
    return next((packs[b] for b in BASELINES if b in packs), None)


def unpacked_path(entry: dict, tmp: Path) -> Path:
    """Where open_pack unpacks a pack. Only the baseline names (or the plain slug shape) get this far."""
    if not SLUG.fullmatch(str(entry.get("country"))):
        sys.exit("odd pack name in the index")
    return tmp / f"{entry['country']}.sqlite"


def open_pack(entry: dict, folder: Path, tmp: Path) -> sqlite3.Connection:
    """Check the file against its index entry, unpack it and open it read-only. The index's file name and sizes
    are checked before they're used, so a bad entry can't point outside `folder` or unpack without limit."""
    name, size, unpacked = entry.get("file"), entry.get("size"), entry.get("uncompressed_size")
    if not (isinstance(name, str) and FILE.fullmatch(name)) or not all(
        type(n) is int and 0 < n <= MAX_UNPACKED for n in (size, unpacked)
    ):
        sys.exit("odd pack entry in the index")
    f = folder / name
    if not f.is_file() or f.is_symlink() or f.stat().st_size != size or sha256(f) != entry.get("sha256"):
        sys.exit(f"{name} isn't the file its index lists")
    db = unpacked_path(entry, tmp)
    if not inflate(f, db, unpacked):
        sys.exit(f"{entry['file']} doesn't unpack to its stated size")
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    con.execute("PRAGMA trusted_schema = OFF")
    return con


def popular(con: sqlite3.Connection, top: int) -> list[tuple[str, str, int]]:
    """The most-scanned products with nutrition: (barcode, name, scans)."""
    return con.execute(
        "SELECT barcode, name, scans FROM products WHERE has_nutrition = 1 ORDER BY scans DESC, barcode LIMIT ?", [top]
    ).fetchall()


def status(con: sqlite3.Connection, codes: list[str]) -> dict[str, int]:
    """Each barcode's has_nutrition in the pack; missing barcodes are left out."""
    out: dict[str, int] = {}
    for i in range(0, len(codes), 500):
        chunk = codes[i : i + 500]
        marks = ",".join("?" * len(chunk))
        out.update(con.execute(f"SELECT barcode, has_nutrition FROM products WHERE barcode IN ({marks})", chunk).fetchall())
    return out


def judge(new: sqlite3.Connection, watch: list[str], previous: list[tuple[str, str, int]], max_gone: float) -> list[str]:
    """Every problem with the new pack, or an empty list when it may be published."""
    problems = []
    found = status(new, watch)
    for code in watch:
        if code not in found:
            problems.append(f"watch-list product {code} is missing")
        elif found[code] != 1:
            problems.append(f"watch-list product {code} has no nutrition")
    if previous:
        found = status(new, [c for c, _, _ in previous])
        gone = [(c, n, s) for c, n, s in previous if found.get(c) != 1]
        share = len(gone) / len(previous)
        print(f"{len(gone)} of the previous pack's {len(previous)} most-scanned products are gone or lost nutrition ({share:.2%})")
        for c, n, s in gone[:25]:
            print(f"  {c}  {s} scans  {n}  ({'no nutrition' if c in found else 'missing'})")
        if share > max_gone:
            problems.append(
                f"{len(gone)} of the {len(previous)} most-scanned products are gone or lost nutrition "
                f"({share:.1%}, the limit is {max_gone:.0%})"
            )
    return problems


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pick-baseline", type=Path, metavar="INDEX")
    ap.add_argument("--dist", type=Path)
    ap.add_argument("--watchlist", type=Path)
    prev = ap.add_mutually_exclusive_group()
    prev.add_argument("--previous-index", type=Path, help="the last published index.json, already signature-checked")
    prev.add_argument("--first-release", action="store_true")
    ap.add_argument("--previous-dir", type=Path)
    ap.add_argument("--top", type=int, default=TOP)
    ap.add_argument("--max-gone", type=float, default=MAX_GONE)
    args = ap.parse_args()

    if args.pick_baseline:
        entry = pick_baseline(json.loads(args.pick_baseline.read_text()))
        if entry is None:
            sys.exit("the previous release has no pack to compare with")
        print(entry["file"])
        return
    if not (args.dist and args.watchlist and (args.first_release or (args.previous_index and args.previous_dir))):
        ap.error("needs --dist, --watchlist and either --first-release or --previous-index with --previous-dir")

    watch = read_watchlist(args.watchlist)
    index = json.loads((args.dist / "index.unsigned.json").read_text())
    with tempfile.TemporaryDirectory() as tmp:
        new = open_pack(index["packs"][0], args.dist, Path(tmp))
        previous: list[tuple[str, str, int]] = []
        if args.previous_index:
            entry = pick_baseline(json.loads(args.previous_index.read_text()))
            if entry is None:
                sys.exit("the previous release has no pack to compare with")
            old = open_pack(entry, args.previous_dir, Path(tmp))
            previous = popular(old, args.top)
            old.close()
            print(f"comparing with the previous {entry['country']} pack's {len(previous)} most-scanned products")
        else:
            print("::warning::No previous release, so only the watch-list was checked")
        problems = judge(new, watch, previous, args.max_gone)
        new.close()
    if problems:
        sys.exit("Not publishing:\n  " + "\n  ".join(problems))
    print(f"safety check passed: all {len(watch)} watch-list products present with nutrition")


if __name__ == "__main__":
    main()
