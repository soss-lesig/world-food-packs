"""Tests for the pack scripts. Run from the repo root with: python -m unittest discover -s tests -v"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import bson  # noqa: E402
import build_packs  # noqa: E402
import extract_dump  # noqa: E402
import popular_gate  # noqa: E402
import verify_packs  # noqa: E402


def gzip_bytes(data: bytes) -> bytes:
    c = zlib.compressobj(9, zlib.DEFLATED, 31)
    return c.compress(data) + c.flush()


class ExtractTests(unittest.TestCase):
    def test_clean_strips_control_and_direction_characters(self):
        self.assertEqual(extract_dump.clean("Cheese‮ reversed\u0000\n  slices", 100), "Cheese reversed slices")
        self.assertEqual(extract_dump.clean("x" * 500, 150), "x" * 150)
        self.assertIsNone(extract_dump.clean("​​", 10))
        self.assertIsNone(extract_dump.clean(42, 10))

    def test_units_must_suit_the_nutrient(self):
        self.assertEqual(extract_dump.factor_for("kcal", "kcal"), 1.0)
        self.assertIsNone(extract_dump.factor_for("kcal", "kJ"))  # kJ in the kcal box is refused, not taken as kcal
        self.assertIsNone(extract_dump.factor_for("kcal", None))
        self.assertIsNone(extract_dump.factor_for("protein", "kcal"))
        self.assertEqual(extract_dump.factor_for("salt", "mg"), 0.001)
        self.assertEqual(extract_dump.factor_for("fat", None), 1.0)

    def test_whole_numbers_are_bounded(self):
        self.assertEqual(extract_dump.whole(10**30), 0)
        self.assertEqual(extract_dump.whole(float("inf")), 0)
        self.assertEqual(extract_dump.whole(-5), 0)
        self.assertEqual(extract_dump.whole("12"), 12)

    def test_decompression_is_bounded(self):
        bomb = gzip_bytes(b"\0" * 64_000_000)  # 64 MB of zeros squeezes to about 60 KB
        stream = extract_dump.HashingGunzip(io.BytesIO(bomb))
        self.assertEqual(len(stream.read(10)), 10)
        self.assertLessEqual(len(stream.buffer), extract_dump.OUT_CHUNK)

    def test_bad_document_length_stops(self):
        for length in (0, 3, extract_dump.MAX_DOC + 1):
            archive = extract_dump.MAGIC + struct.pack("<I", length) + b"\0" * 8
            stream = extract_dump.HashingGunzip(io.BytesIO(gzip_bytes(archive)))
            with self.assertRaises(SystemExit):
                list(extract_dump.documents(stream))

    def test_country_tags_are_not_needed(self):
        # The 28 October 2026 dump had wrong or missing countries on popular products: they must still get in.
        doc = {"code": "90162800", "product_name": "Red Bull Sugarfree", "nutriments": {"energy-kcal_100g": 3, "proteins_100g": 0,
               "carbohydrates_100g": 0, "fat_100g": 0}}
        for countries in ([], None, ["en:france"]):
            raw = bson.encode({**doc, **({"countries_tags": countries} if countries is not None else {})})
            row = extract_dump.slim(raw)
            self.assertEqual((row["code"], row["kcal"]), ("90162800", 3.0), countries)
            self.assertNotIn("countries", row)

    def test_failed_run_leaves_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "bad.gz"
            src.write_bytes(gzip_bytes(extract_dump.MAGIC + struct.pack("<I", 2)))
            out = Path(tmp) / "slim.jsonl"
            r = subprocess.run([sys.executable, "-I", "scripts/extract_dump.py", str(src), "--out", str(out)], capture_output=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["bad.gz"])


class BuildTests(unittest.TestCase):
    """A tiny slim file through the real build query."""

    ROWS = [
        dict(code="5000000000011", name="Yoghurt", kcal=60, kj=251, protein=4, carbs=6, fat=3),
        # kJ typed into the kcal box: 251 kcal against macros adding up to 67.
        dict(code="5000000000012", name="Yoghurt slip", kcal=251, protein=4, carbs=6, fat=3),
        # kcal typed into the kJ box, no kcal: 60 / 4.184 = 14 kcal against macros of 67.
        dict(code="5000000000013", name="Yoghurt slip two", kj=60, protein=4, carbs=6, fat=3),
        # Wine: almost no macros, the energy is alcohol.
        dict(code="5000000000014", name="Red wine", kcal=85, protein=0.1, carbs=2.6, fat=0, alcohol=13, per="100ml"),
        # No country at all, or an old slim line that still has one: countries are ignored.
        dict(code="90162800", name="Red Bull Sugarfree", kcal=3, protein=0, carbs=0, fat=0, per="100ml"),
        dict(code="5000000000015", name="Old line", countries=["en:yugoslavia"], kcal=100, protein=1, carbs=20, fat=1),
        # Not usable: energy without fat, energy alone, a name with no nutrition.
        dict(code="5000000000016", name="No fat given", kcal=100, protein=1, carbs=20),
        dict(code="5000000000017", name="Energy only", kcal=100),
        dict(code="5000000000018", name="Nothing"),
        # The newest record of a barcode decides, even if an older one had nutrition.
        dict(code="5000000000019", name="Edited away", kcal=100, protein=1, carbs=20, fat=1, updated_at=1),
        dict(code="5000000000019", name="Edited away", updated_at=2),
    ]

    def build(self, t: Path, *extra: str, watch: str = "5000000000017\n") -> sqlite3.Connection:
        """Run the real build on ROWS; returns the unpacked pack."""
        with (t / "slim.jsonl").open("w") as f:
            for r in self.ROWS:
                f.write(json.dumps({"scans": 1, "updated_at": 1, "obsolete": False, **r}) + "\n")
        (t / "watch.txt").write_text(watch)
        r = subprocess.run([sys.executable, "-I", "scripts/build_packs.py", "--slim", str(t / "slim.jsonl"),
                            "--source-sha256", "0" * 64, "--out", str(t / "dist"), "--watchlist", str(t / "watch.txt"),
                            *extra], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        db = t / "world.sqlite"
        db.write_bytes(zlib.decompress((t / "dist/world.sqlite.deflate").read_bytes(), -15))
        return sqlite3.connect(db)

    def test_build_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            con = self.build(t, "--first-release", "--top", "0")
            files = sorted(p.name for p in (t / "dist").iterdir())
            self.assertEqual(files, ["index.unsigned.json", "world.sqlite.deflate"], "One worldwide pack, nothing else")
            got = dict(con.execute("SELECT name, has_nutrition FROM products").fetchall())
            per_ml = dict(con.execute("SELECT name, per_ml FROM products").fetchall())
            con.close()
            # Usable nutrition gets in; "Energy only" is on the watch-list, so it's kept with what it has.
            self.assertEqual(got, {"Yoghurt": 1, "Red wine": 1, "Red Bull Sugarfree": 1, "Old line": 1, "Energy only": 1})
            self.assertEqual(per_ml["Red wine"], 1)
            index = json.loads((t / "dist/index.unsigned.json").read_text())
            self.assertEqual([(p["country"], p["product_count"], p["products_with_nutrition"]) for p in index["packs"]],
                             [("world", 5, 5)])
            self.assertEqual(verify_packs.shape(index), [])
            self.assertEqual(verify_packs.check_pack(index["packs"][0], t / "dist"), [])

    def test_most_scanned_are_kept_whatever_their_nutrition(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Every row has one scan, so the top 3 are the first three barcodes, implausible ones included.
            con = self.build(Path(tmp), "--first-release", "--top", "3", watch="")
            got = dict(con.execute("SELECT barcode, has_nutrition FROM products").fetchall())
            con.close()
            self.assertEqual(got.get("5000000000012"), 0, "Kept, with its impossible nutrition blanked")
            self.assertNotIn("5000000000018", got)

    def test_last_weeks_nutrition_and_popular_products_are_carried_forward(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            prev = t / "prev"
            prev.mkdir()
            full = (1, 100.0, 1.0, 20.0, 1.0, 1.0, 1.0, 0.1)
            rows = [
                # This week's dump lost its fat, and lost all of its nutrition: last week's comes back.
                ("5000000000016", "No fat given", None, None, None, None, *full, 0, 50, 0),
                ("5000000000019", "Edited away", None, None, None, None, *full, 0, 40, 0),
                # Popular last week, gone from this week's dump: it comes back whole.
                ("5000000000099", "Gone from the dump", None, None, None, None, *full, 0, 60, 0),
                # Not popular and gone: stays gone.
                ("5000000000098", "Rarely scanned", None, None, None, None, *full, 0, 0, 0),
                # Had some nutrition last week, still partial this week, not popular: still kept.
                ("5000000000017", "Energy only", None, None, None, None, 1, 100.0, *[None] * 6, 0, 0, 0),
            ]
            entry = make_pack(prev, rows=rows)
            (prev / "index.json").write_text(json.dumps({"packs": [entry]}))
            con = self.build(t, "--previous-index", str(prev / "index.json"), "--previous-dir", str(prev), "--top", "3",
                             watch="")
            self.assertIn("5000000000017", {r[0] for r in con.execute("SELECT barcode FROM products")})
            got = {r[0]: r[1:] for r in con.execute("SELECT barcode, has_nutrition, fat FROM products")}
            con.close()
            self.assertEqual(got["5000000000016"], (1, 1.0))
            self.assertEqual(got["5000000000019"], (1, 1.0))
            self.assertIn("5000000000099", got)
            self.assertNotIn("5000000000098", got)

    def test_schema_definitions_are_exact(self):
        # A non-breaking space glues "INTEGER PRIMARY" into one type name, quietly dropping constraints.
        self.assertIsNone(verify_packs.normalise_sql("CREATE TABLE meta (key TEXT\u00a0PRIMARY KEY, value TEXT NOT NULL)"))
        self.assertEqual(verify_packs.expected_sql()["meta"], "create table meta ( key text primary key , value text not null )")

    def test_alcohol_is_in_percent(self):
        self.assertEqual(extract_dump.factor_for("alcohol", "% vol"), 1.0)
        self.assertIsNone(extract_dump.factor_for("alcohol", "g"))


def make_pack(folder: Path, country: str = "world", rows: list[tuple] | None = None, hostile: str | None = None) -> dict:
    rows = rows or [
        ("5000000000001", "Oat milk", "Brand", "1 l", None, None, 1, 46.0, 1.0, 6.6, 4.0, 1.5, 0.8, 0.1, 0, 3, 1),
        ("5000000000002", "Cheddar", None, "200 g", None, 30.0, 1, 416.0, 25.0, 0.1, 0.1, 34.9, 0.0, 1.8, 0, 9, 0),
    ]
    db = folder / f"{country}.sqlite"
    build_packs.write_pack(rows, db, {"country": country})
    if hostile:
        con = sqlite3.connect(db)
        con.executescript(hostile)
        con.commit()
        con.close()
    pack = folder / f"{country}.sqlite.deflate"
    build_packs.deflate(db, pack)
    entry = {
        "country": country, "name": build_packs.PACK_NAME, "file": pack.name,
        "product_count": len(rows), "products_with_nutrition": sum(r[6] for r in rows),
        "size": pack.stat().st_size, "uncompressed_size": db.stat().st_size, "sha256": build_packs.sha256(pack),
    }
    db.unlink()
    return entry


def make_index(packs: list[dict]) -> dict:
    return {
        "schema_version": 3, "version": 20261028, "built_at": "2026-10-28T00:00:00+00:00", "source": "s", "licence": "l",
        "source_file": "f", "source_sha256": "0" * 64, "compression": "deflate-raw", "signature_algorithm": "ed25519",
        "packs": packs,
    }


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dist = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_good_pack_passes(self):
        entry = make_pack(self.dist)
        self.assertEqual(verify_packs.shape(make_index([entry])), [])
        self.assertEqual(verify_packs.check_pack(entry, self.dist), [])

    def test_country_names_must_be_plain_slugs(self):
        for country, file in [("Türkiye", "Türkiye.sqlite.deflate"), ("../x", "../x.sqlite.deflate"),
                              ("a b", "a b.sqlite.deflate"), ("uk", "other.sqlite.deflate"), ("uk", "/tmp/uk.sqlite.deflate")]:
            entry = make_pack(self.dist)
            entry.update(country=country, file=file)
            self.assertNotEqual(verify_packs.shape(make_index([entry])), [], country)

    def test_extra_fields_are_refused(self):
        entry = make_pack(self.dist)
        index = make_index([{**entry, "url": "https://evil.example"}])
        self.assertNotEqual(verify_packs.shape(index), [])
        self.assertNotEqual(verify_packs.shape({**make_index([entry]), "extra": 1}), [])

    def test_blank_required_fields_are_caught(self):
        # The real schema refuses blanks outright; a mistyped value still gets in, so verify checks types too.
        mistyped = ("5000000000004", "Oats", None, None, None, None, 1, 100.0, 1, 1, 1, 1, 1, 1, 0, "lots", 0)
        entry = make_pack(self.dist, rows=[mistyped])
        self.assertTrue(any("missing or mistyped" in p for p in verify_packs.check_pack(entry, self.dist)))

    def test_views_and_triggers_are_refused(self):
        for sql in ("CREATE VIEW v AS SELECT 1;",
                    "CREATE TRIGGER t AFTER INSERT ON meta BEGIN SELECT 1; END;",
                    "CREATE TABLE extra (x);"):
            entry = make_pack(self.dist, hostile=sql)
            problems = verify_packs.check_pack(entry, self.dist)
            self.assertTrue(any("unexpected database objects" in p for p in problems), sql)

    def test_tables_must_be_defined_exactly(self):
        # Same table names, different definition: a pack built by anything but build_packs.py.
        entry = make_pack(self.dist, hostile="DROP TABLE meta; CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT 'x');")
        self.assertTrue(any("defined exactly" in p for p in verify_packs.check_pack(entry, self.dist)))

    def test_counts_are_recounted_not_trusted(self):
        entry = make_pack(self.dist)
        entry["product_count"] = 999
        self.assertTrue(verify_packs.check_pack(entry, self.dist))

    def test_bad_values_are_caught(self):
        bad = ("5000000000003", "Yoghurt", None, None, None, None, 1, 950.0, 1, 1, 1, 1, 1, 1, 0, 0, 0)
        entry = make_pack(self.dist, rows=[bad])
        self.assertTrue(any("energy range" in p for p in verify_packs.check_pack(entry, self.dist)))

    def test_unpacking_stops_at_the_stated_size(self):
        entry = make_pack(self.dist)
        self.assertFalse(verify_packs.inflate(self.dist / entry["file"], self.dist / "out", entry["uncompressed_size"] - 1))

    def test_a_kept_product_without_fat_passes(self):
        no_fat = ("5000000000005", "Oats", None, None, None, None, 1, 100.0, 1.0, 1.0, 1.0, None, 1.0, 1.0, 0, 0, 0)
        entry = make_pack(self.dist, rows=[no_fat])
        self.assertEqual(verify_packs.check_pack(entry, self.dist), [])


class GateTests(unittest.TestCase):
    """verify_packs.gates: the one worldwide pack, almost all with nutrition, no big shrink against last week."""

    @staticmethod
    def index(*packs: tuple[str, int, int]) -> dict:
        return {"packs": [{"country": c, "product_count": n, "products_with_nutrition": w} for c, n, w in packs]}

    def gates(self, index, previous=None, minimum=100):
        return verify_packs.gates(index, previous, argparse.Namespace(min_products=minimum))

    def test_only_the_world_pack(self):
        self.assertEqual(self.gates(self.index(("world", 1000, 1000))), [])
        self.assertTrue(self.gates(self.index(("world", 1000, 1000), ("united-kingdom", 10, 10))))
        self.assertTrue(self.gates(self.index(("united-kingdom", 1000, 1000))))

    def test_minimum_and_nutrition(self):
        self.assertTrue(self.gates(self.index(("world", 99, 99))))
        # Only the must-keep products may lack nutrition: up to 1%.
        self.assertEqual(self.gates(self.index(("world", 1000, 990))), [])
        self.assertTrue(self.gates(self.index(("world", 1000, 989))))

    def test_shrink_against_last_week(self):
        world = self.index(("world", 950, 950))
        self.assertEqual(self.gates(world, self.index(("world", 1000, 1000))), [])
        self.assertTrue(self.gates(self.index(("world", 949, 949)), self.index(("world", 1000, 1000))))
        # Changing over from country packs: the biggest country's products with nutrition are the floor.
        countries = self.index(("france", 1500, 900), ("united-kingdom", 200, 150))
        self.assertEqual(self.gates(world, countries), [])
        self.assertTrue(self.gates(self.index(("world", 800, 800)), countries))


class PopularGateTests(unittest.TestCase):
    """popular_gate: the watch-list and the previous pack's most-scanned products."""

    @staticmethod
    def row(code: str, scans: int, nutrition: bool = True) -> tuple:
        n = (100.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0) if nutrition else (None,) * 7
        return (code, f"Food {code}", None, None, None, None, 1 if nutrition else 0, *n, 0, scans, 0)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def pack(self, name: str, rows: list[tuple], country: str = "world"):
        folder = self.t / name
        folder.mkdir()
        entry = make_pack(folder, country=country, rows=rows)
        return entry, popular_gate.open_pack(entry, folder, folder)

    def test_watchlist_must_be_present_with_nutrition(self):
        _, new = self.pack("new", [self.row("90162800", 5), self.row("5449000000996", 9, nutrition=False)])
        self.assertEqual(popular_gate.judge(new, ["90162800"], [], 0.01), [])
        problems = popular_gate.judge(new, ["90162800", "5449000000996", "3017620422003"], [], 0.01)
        self.assertEqual(len(problems), 2, problems)
        new.close()

    def test_popular_products_from_last_week(self):
        old_rows = [self.row(f"{5000000000100 + i}", 1000 - i) for i in range(100)]
        _, old = self.pack("old", old_rows + [self.row("5000000000999", 5000, nutrition=False)])
        top = popular_gate.popular(old, 50)
        self.assertEqual(len(top), 50, "Only products with nutrition count as popular")
        self.assertEqual(top[0][0], "5000000000100")
        old.close()
        # One of the top 50 gone (2%) passes a 5% limit and fails a 1% one.
        _, new = self.pack("new", old_rows[1:])
        self.assertEqual(popular_gate.judge(new, [], top, 0.05), [])
        self.assertTrue(popular_gate.judge(new, [], top, 0.01))
        new.close()

    def test_baseline_is_the_world_pack_then_the_uk(self):
        uk, world = {"country": "united-kingdom"}, {"country": "world"}
        self.assertEqual(popular_gate.pick_baseline({"packs": [{"country": "france"}, uk]}), uk)
        self.assertEqual(popular_gate.pick_baseline({"packs": [uk, world]}), world)
        self.assertIsNone(popular_gate.pick_baseline({"packs": [{"country": "france"}]}))

    def test_tampered_previous_pack_is_refused(self):
        entry, con = self.pack("old", [self.row("5000000000001", 1)])
        con.close()
        with (self.t / "old" / entry["file"]).open("ab") as f:
            f.write(b"x")
        with self.assertRaises(SystemExit):
            popular_gate.open_pack(entry, self.t / "old", self.t)

    def test_odd_index_entries_are_refused_before_use(self):
        entry, con = self.pack("old", [self.row("5000000000001", 1)])
        con.close()
        for bad in ({"file": "../old/world.sqlite.deflate"}, {"file": "/etc/passwd"}, {"uncompressed_size": 10**12},
                    {"size": "12"}, {"country": "../x"}):
            with self.assertRaises(SystemExit, msg=bad):
                popular_gate.open_pack({**entry, **bad}, self.t / "old", self.t)

    def test_any_popular_product_gone_stops_the_release(self):
        self.assertEqual(popular_gate.MAX_GONE, 0)

    def test_watchlist_file(self):
        f = self.t / "watch.txt"
        f.write_text("# Must always be there\n90162800  # Red Bull Sugarfree\n\n3017620422003\n")
        self.assertEqual(popular_gate.read_watchlist(f), ["90162800", "3017620422003"])
        f.write_text("9016280O\n")
        with self.assertRaises(SystemExit):
            popular_gate.read_watchlist(f)

    def test_the_real_watchlist_reads(self):
        codes = popular_gate.read_watchlist(Path(__file__).resolve().parent.parent / "popular-watchlist.txt")
        self.assertIn("90162800", codes)


def has_openssl3() -> bool:
    return bool(shutil.which("openssl")) and b"OpenSSL 3" in subprocess.run(["openssl", "version"], capture_output=True).stdout


@unittest.skipUnless(has_openssl3(), "needs OpenSSL 3 for Ed25519")
class SignAndStageTests(unittest.TestCase):
    def run_script(self, *args):
        return subprocess.run([sys.executable, "-I", *map(str, args)], capture_output=True, text=True)

    def keys(self, t: Path):
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", t / "key.pem"], check=True)
        subprocess.run(["openssl", "pkey", "-in", t / "key.pem", "-pubout", "-out", t / "pub.pem"], check=True)

    def test_round_trip_and_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            dist = t / "dist"
            dist.mkdir()
            (dist / "index.unsigned.json").write_text(json.dumps(make_index([make_pack(dist)])))
            self.keys(t)
            signed = self.run_script("scripts/sign_packs.py", "--dist", dist, "--key", t / "key.pem", "--out", t / "signed",
                                     "--expect-index-sha256", build_packs.sha256(dist / "index.unsigned.json"))
            self.assertEqual(signed.returncode, 0, signed.stderr)
            signed_sha = build_packs.sha256(t / "signed" / "index.json")
            stale = self.run_script("scripts/stage_release.py", "--dist", dist, "--signed", t / "signed", "--public", t / "pub.pem",
                                    "--release", t / "rel0", "--expect-index-sha256", "0" * 64)
            self.assertNotEqual(stale.returncode, 0, "Only the index this run signed")
            ok = self.run_script("scripts/stage_release.py", "--dist", dist, "--signed", t / "signed", "--public", t / "pub.pem",
                                 "--release", t / "rel", "--expect-index-sha256", signed_sha)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertEqual(sorted(p.name for p in (t / "rel").iterdir()),
                             ["index.json", "index.json.sig", "world.sqlite.deflate"])
            # A pack changed after signing must not be published.
            with (dist / "world.sqlite.deflate").open("ab") as f:
                f.write(b"x")
            bad = self.run_script("scripts/stage_release.py", "--dist", dist, "--signed", t / "signed", "--public", t / "pub.pem",
                                  "--release", t / "rel2", "--expect-index-sha256", signed_sha)
            self.assertNotEqual(bad.returncode, 0)

    def test_signing_refuses_an_index_that_wasnt_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            dist = t / "dist"
            dist.mkdir()
            (dist / "index.unsigned.json").write_text(json.dumps(make_index([make_pack(dist)])))
            self.keys(t)
            wrong = self.run_script("scripts/sign_packs.py", "--dist", dist, "--key", t / "key.pem", "--out", t / "s",
                                    "--expect-index-sha256", "0" * 64)
            self.assertNotEqual(wrong.returncode, 0)
            right = self.run_script("scripts/sign_packs.py", "--dist", dist, "--key", t / "key.pem", "--out", t / "s",
                                    "--expect-index-sha256", build_packs.sha256(dist / "index.unsigned.json"))
            self.assertEqual(right.returncode, 0, right.stderr)

    def test_signing_refuses_unlisted_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            dist = t / "dist"
            dist.mkdir()
            (dist / "index.unsigned.json").write_text(json.dumps(make_index([make_pack(dist)])))
            (dist / "extra.bin").write_bytes(b"surprise")
            self.keys(t)
            r = self.run_script("scripts/sign_packs.py", "--dist", dist, "--key", t / "key.pem", "--out", t / "s",
                                "--expect-index-sha256", build_packs.sha256(dist / "index.unsigned.json"))
            self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
