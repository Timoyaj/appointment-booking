-- Stage 3: recurring agreements.
--
-- A series adopts an existing booking as occurrence zero and generates the rest.
-- The occurrences are ordinary reservations -- they occupy tables, appear in
-- listings and keep their own records -- so this stores only the agreement itself
-- and which booking belongs to which position in it.
--
-- A reference is unique across occurrences, which is what makes "already adopted"
-- a lookup rather than a judgement. `idx` is a position that never changes: dates
-- and tables move, references and indices do not.

CREATE TABLE IF NOT EXISTS series (
    id               TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    restaurant_id    TEXT NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    anchor_reference TEXT NOT NULL,
    count            INTEGER NOT NULL,
    interval_weeks   INTEGER NOT NULL,
    revision         INTEGER NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS series_occurrences (
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    idx       INTEGER NOT NULL,
    reference TEXT NOT NULL UNIQUE,
    exception INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (series_id, idx)
);

CREATE INDEX IF NOT EXISTS idx_series_occurrences_reference ON series_occurrences (reference);
