PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS products (
    sku           TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    name_norm     TEXT NOT NULL,
    brand         TEXT,
    size_text     TEXT,
    last_price    REAL,
    last_seen_at  TEXT NOT NULL,
    image_url     TEXT,
    raw_url       TEXT,
    is_available  INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_products_norm ON products(name_norm);
CREATE INDEX IF NOT EXISTS idx_products_brand ON products(brand);

CREATE TABLE IF NOT EXISTS purchases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      TEXT NOT NULL,
    sku           TEXT NOT NULL,
    qty           REAL NOT NULL,
    unit_price    REAL,
    purchased_at  TEXT NOT NULL,
    UNIQUE(order_id, sku),
    FOREIGN KEY (sku) REFERENCES products(sku)
);
CREATE INDEX IF NOT EXISTS idx_purchases_sku ON purchases(sku);
CREATE INDEX IF NOT EXISTS idx_purchases_date ON purchases(purchased_at);

CREATE TABLE IF NOT EXISTS generic_aliases (
    generic_name        TEXT PRIMARY KEY,
    generic_name_norm   TEXT NOT NULL,
    preferred_sku       TEXT NOT NULL,
    confidence          REAL NOT NULL,
    source              TEXT NOT NULL,
    last_confirmed_at   TEXT NOT NULL,
    times_used          INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (preferred_sku) REFERENCES products(sku)
);
CREATE INDEX IF NOT EXISTS idx_alias_norm ON generic_aliases(generic_name_norm);

CREATE TABLE IF NOT EXISTS search_cache (
    query         TEXT PRIMARY KEY,
    results_json  TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    ttl_seconds   INTEGER NOT NULL DEFAULT 3600
);

CREATE TABLE IF NOT EXISTS pending_questions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL,
    generic_name    TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    chosen_sku      TEXT,
    state           TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    answered_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_pq_job ON pending_questions(job_id);
CREATE INDEX IF NOT EXISTS idx_pq_state ON pending_questions(state);

CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    state         TEXT NOT NULL,
    raw_list_json TEXT NOT NULL,
    plan_json     TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
