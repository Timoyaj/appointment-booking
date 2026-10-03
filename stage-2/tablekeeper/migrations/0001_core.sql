-- Tablekeeper schema.
--
-- `ordinal` columns preserve fixture order, which the API depends on:
-- available_table_ids is listed "in fixture order".
--
-- Timestamps are stored as the exact RFC 3339 strings the API returns, so
-- export/import can restore state without regenerating an identity or a time.

CREATE TABLE IF NOT EXISTS users (
    id           TEXT PRIMARY KEY,
    email        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password     TEXT NOT NULL,
    display_name TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS restaurants (
    id                             TEXT PRIMARY KEY,
    name                           TEXT NOT NULL,
    timezone                       TEXT NOT NULL,
    slot_minutes                   INTEGER NOT NULL,
    reservation_duration_minutes   INTEGER NOT NULL,
    cancellation_cutoff_minutes    INTEGER NOT NULL,
    ordinal                        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS opening_hours (
    restaurant_id TEXT NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    weekday       TEXT NOT NULL,
    opens         TEXT NOT NULL,
    closes        TEXT NOT NULL,
    ordinal       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hours_restaurant ON opening_hours (restaurant_id, ordinal);

CREATE TABLE IF NOT EXISTS dining_tables (
    restaurant_id TEXT NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    id            TEXT NOT NULL,
    label         TEXT NOT NULL,
    capacity      INTEGER NOT NULL,
    ordinal       INTEGER NOT NULL,
    PRIMARY KEY (restaurant_id, id)
);

CREATE TABLE IF NOT EXISTS reservations (
    id              TEXT PRIMARY KEY,
    reference       TEXT NOT NULL UNIQUE,
    user_id         TEXT NOT NULL,
    restaurant_id   TEXT NOT NULL,
    table_id        TEXT NOT NULL,
    party_size      INTEGER NOT NULL,
    status          TEXT NOT NULL,
    starts_at_local TEXT NOT NULL,
    starts_at       TEXT NOT NULL,
    ends_at         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    -- starts_at/ends_at carry the restaurant's local offset, so their string
    -- order is NOT chronological across a DST change. These two are always UTC
    -- and fixed-width, which makes lexicographic comparison correct for range
    -- queries and ordering.
    starts_at_utc   TEXT NOT NULL,
    ends_at_utc     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reservations_user ON reservations (user_id, starts_at_utc);
CREATE INDEX IF NOT EXISTS idx_reservations_table
    ON reservations (restaurant_id, table_id, status, starts_at_utc);

-- One row per completed idempotent operation. The key is scoped to the user and
-- to the method+path, so the same key string on a different path is a different
-- request. Failed (4xx) requests are deliberately never recorded: the spec says
-- a key reused after a failure is treated as a first use.
CREATE TABLE IF NOT EXISTS idempotency (
    user_id          TEXT NOT NULL,
    key              TEXT NOT NULL,
    method           TEXT NOT NULL,
    path             TEXT NOT NULL,
    body_fingerprint TEXT NOT NULL,
    request_body     TEXT NOT NULL,
    status_code      INTEGER NOT NULL,
    response_body    TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (user_id, key, method, path)
);
