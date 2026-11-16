-- Full rebuild. The ingest command runs this file before every load, so
-- each reload starts from the CSV alone.

DROP VIEW IF EXISTS clean_transactions;
DROP TABLE IF EXISTS data_quality_issues;
DROP TABLE IF EXISTS transactions;
DROP TABLE IF EXISTS clients;
DROP TABLE IF EXISTS securities;
DROP TABLE IF EXISTS distributors;
DROP TABLE IF EXISTS raw_transactions;


-- The data exactly as received, stored as text with one row per CSV line.
-- Rows are never deleted, so every cleaning decision can be traced back.
CREATE TABLE raw_transactions (
    source_row       INTEGER PRIMARY KEY,  -- line number in the CSV (header = 1)
    transaction_id   TEXT,
    date             TEXT,
    client_id        TEXT,
    client_name      TEXT,
    fund_type        TEXT,
    distributor      TEXT,
    stock_code       TEXT,
    stock_name       TEXT,
    transaction_type TEXT,
    quantity         TEXT,
    price_per_unit   TEXT,
    total_value      TEXT,
    loaded_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);


CREATE TABLE distributors (
    id   SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

-- In this file each client belongs to exactly one distributor and one fund
-- structure, so both are stored on the client rather than on each transaction.
CREATE TABLE clients (
    client_id      TEXT PRIMARY KEY,
    client_name    TEXT NOT NULL,
    fund_type      TEXT NOT NULL CHECK (fund_type IN ('PMS', 'AIF')),
    distributor_id INTEGER NOT NULL REFERENCES distributors (id)
);

CREATE TABLE securities (
    stock_code TEXT PRIMARY KEY,
    stock_name TEXT NOT NULL
);


CREATE TABLE transactions (
    transaction_id   TEXT PRIMARY KEY,
    source_row       INTEGER NOT NULL REFERENCES raw_transactions (source_row),
    trade_date       DATE NOT NULL,
    client_id        TEXT NOT NULL REFERENCES clients (client_id),
    stock_code       TEXT NOT NULL REFERENCES securities (stock_code),
    -- Stored as received (BUY / PURCHASE / SELL / REDEMPTION), so the
    -- distinction is preserved if the business later treats them differently.
    transaction_type TEXT NOT NULL
        CHECK (transaction_type IN ('BUY', 'PURCHASE', 'SELL', 'REDEMPTION')),
    -- +1 = money into the portfolio, -1 = money out
    direction        SMALLINT NOT NULL CHECK (direction IN (1, -1)),
    quantity         INTEGER NOT NULL CHECK (quantity > 0),
    price_per_unit   NUMERIC(12, 2) NOT NULL CHECK (price_per_unit > 0),
    total_value      NUMERIC(16, 2) NOT NULL CHECK (total_value > 0),
    -- Excluded rows remain in the table, so a decision can be reversed by
    -- changing a flag rather than re-running the whole pipeline.
    is_excluded      BOOLEAN NOT NULL DEFAULT FALSE,
    exclusion_reason TEXT,
    CHECK (is_excluded = (exclusion_reason IS NOT NULL))
);

CREATE INDEX idx_transactions_client_date ON transactions (client_id, trade_date);
CREATE INDEX idx_transactions_date ON transactions (trade_date);
CREATE INDEX idx_transactions_stock_date ON transactions (stock_code, trade_date);


CREATE TABLE data_quality_issues (
    id             SERIAL PRIMARY KEY,
    source_row     INTEGER NOT NULL REFERENCES raw_transactions (source_row),
    transaction_id TEXT,
    field          TEXT NOT NULL,
    issue_type     TEXT NOT NULL,
    description    TEXT NOT NULL,
    action         TEXT NOT NULL CHECK (action IN ('fixed', 'excluded', 'flagged')),
    original_value TEXT,
    new_value      TEXT
);

CREATE INDEX idx_dq_source_row ON data_quality_issues (source_row);


-- All reports read from this view, so the exclusion filter is applied consistently.
CREATE VIEW clean_transactions AS
SELECT *
FROM transactions
WHERE NOT is_excluded;
