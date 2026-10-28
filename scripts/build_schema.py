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

# Every table exactly as it's stored, normalised (comments and layout removed, lower case). The app
# (FoodDatabase.expectedSQL) holds the same list. If SQLite ever writes these differently, the verify job
# fails instead of phones quietly refusing the packs.
EXPECTED_SQL = {
    "meta": "create table meta ( key text primary key , value text not null )",
    "products": (
        "create table products ( id integer primary key , barcode text not null unique , name text not null , brand text , "
        "quantity text , serving_size text , serving_g real , has_nutrition integer not null , kcal real , protein real , "
        "carbs real , sugar real , fat real , fibre real , salt real , updated_at integer , scans integer not null default 0 , "
        "per_ml integer not null default 0 )"
    ),
    "products_fts": (
        "create virtual table products_fts using fts5 ( name , brand , content = 'products' , content_rowid = 'id' , "
        "tokenize = 'unicode61 remove_diacritics 2' , prefix = '2 3' )"
    ),
    "products_fts_config": "create table 'products_fts_config' ( k primary key , v ) without rowid",
    "products_fts_data": "create table 'products_fts_data' ( id integer primary key , block blob )",
    "products_fts_docsize": "create table 'products_fts_docsize' ( id integer primary key , sz blob )",
    "products_fts_idx": "create table 'products_fts_idx' ( segid , term , pgno , primary key ( segid , term ) ) without rowid",
}
