-- Stage 4: table closures, and the seating plans that propose and record them.
--
-- A closure is a fact about a table over an interval: from the moment a plan is
-- applied, that table cannot be seated at, alone or as part of a pair, for the
-- whole half-open interval. It is stored normalised to UTC as well as exactly as
-- the manager wrote it, so an availability query can compare it against a
-- sitting with the same fixed-width, chronologically-ordered strings it already
-- uses for bookings, while the response echoes the manager's own instants.
--
-- A plan is stored the moment it is previewed and is only ever *proposed*: it
-- changes nothing else. Applying it writes the closure and every reassignment in
-- one transaction, marks the plan applied, and is guarded by the restaurant's
-- revision, which is what makes "anything happened in between" a single
-- comparison rather than a re-planning problem.
--
-- `reservation_history.plan_id` is nullable and is set only on the entries a plan
-- application writes, so every entry recorded before this stage keeps exactly the
-- shape it had.

CREATE TABLE IF NOT EXISTS replans (
    id                  TEXT PRIMARY KEY,
    restaurant_id       TEXT NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    table_id            TEXT NOT NULL,
    closed_from         TEXT NOT NULL,
    closed_to           TEXT NOT NULL,
    closed_from_utc     TEXT NOT NULL,
    closed_to_utc       TEXT NOT NULL,
    -- The restaurant's revision when the plan was previewed. A plan is stale the
    -- moment this is not the current revision, whatever changed it.
    restaurant_revision INTEGER NOT NULL,
    status              TEXT NOT NULL,
    moved_count         INTEGER NOT NULL,
    unused_seats        INTEGER NOT NULL,
    created_at          TEXT NOT NULL,
    applied_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_replans_restaurant ON replans (restaurant_id, created_at);

-- One row per considered booking, in the order the API reports them: ascending
-- reservation reference. A single member leaves `table_b` NULL.
CREATE TABLE IF NOT EXISTS replan_assignments (
    plan_id   TEXT NOT NULL REFERENCES replans(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL,
    reference TEXT NOT NULL,
    table_a   TEXT NOT NULL,
    table_b   TEXT,
    changed   INTEGER NOT NULL,
    PRIMARY KEY (plan_id, position)
);

CREATE INDEX IF NOT EXISTS idx_replan_assignments_reference
    ON replan_assignments (reference);

-- A closure belongs to the plan that produced it, one to one: a plan is applied
-- at most once, and a closure exists only because one was.
CREATE TABLE IF NOT EXISTS table_closures (
    plan_id         TEXT PRIMARY KEY REFERENCES replans(id) ON DELETE CASCADE,
    restaurant_id   TEXT NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    table_id        TEXT NOT NULL,
    closed_from     TEXT NOT NULL,
    closed_to       TEXT NOT NULL,
    closed_from_utc TEXT NOT NULL,
    closed_to_utc   TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_closures_restaurant
    ON table_closures (restaurant_id, table_id, closed_from_utc);

ALTER TABLE reservation_history ADD COLUMN plan_id TEXT;
