"""The pack database schema, shared by build_packs.py and verify_packs.py."""

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE products (
  id           INTEGER PRIMARY KEY,
  barcode      TEXT NOT NULL UNIQUE,
  name         TEXT NOT NULL,
  brand        TEXT,
  quantity     TEXT,
  serving_size TEXT,
  serving_g    REAL,
  has_nutrition INTEGER NOT NULL,  -- 1 if the per-100 g values below are usable
  kcal REAL, protein REAL, carbs REAL, sugar REAL, fat REAL, fibre REAL, salt REAL,
  updated_at   INTEGER,
  scans        INTEGER NOT NULL DEFAULT 0,  -- unique scans on Open Food Facts: how popular it is
  per_ml       INTEGER NOT NULL DEFAULT 0   -- 1 if the values are per 100 ml rather than per 100 g
);
CREATE VIRTUAL TABLE products_fts USING fts5(
  name, brand,
  content = 'products', content_rowid = 'id',
  tokenize = 'unicode61 remove_diacritics 2',
  prefix = '2 3'
);
"""
