-- Stage 3: published policies, who may publish them, what a reservation accepted
-- when it was decided, and the ledger of its own record.
--
-- A policy is a complete, immutable set of rules with the date it takes effect
-- from. Policy 0 is not stored: it is the restaurant's own fixture configuration,
-- which is why `restaurants` keeps its original columns and so does
-- `dining_tables.capacity` -- the detail endpoint reports the fixture, while every
-- decision reports the policy that was selected.
--
-- `reservations.revision` and `accepted_terms` are a reservation's own state: the
-- terms it was decided under are frozen with it, so publishing a policy never
-- edits a booking, and a cancellation is judged by the cutoff the diner accepted.

ALTER TABLE restaurants ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;

ALTER TABLE reservations ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE reservations ADD COLUMN accepted_terms TEXT NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS restaurant_managers (
    restaurant_id TEXT NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    position      INTEGER NOT NULL,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (restaurant_id, position)
);

CREATE TABLE IF NOT EXISTS restaurant_policies (
    restaurant_id                  TEXT NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    policy_version                 INTEGER NOT NULL,
    effective_from                 TEXT NOT NULL,
    slot_minutes                   INTEGER NOT NULL,
    reservation_duration_minutes   INTEGER NOT NULL,
    cancellation_cutoff_minutes    INTEGER NOT NULL,
    opening_hours                  TEXT NOT NULL,
    capacities                     TEXT NOT NULL,
    created_at                     TEXT NOT NULL,
    PRIMARY KEY (restaurant_id, policy_version)
);

CREATE INDEX IF NOT EXISTS idx_policies_restaurant
    ON restaurant_policies (restaurant_id, policy_version);

-- One row per event in a reservation's own record, oldest first. `seq` is total
-- even when two writes land in the same second, and every entry carries the
-- revision and the complete accepted terms that resulted from it: an old entry
-- never acquires newer terms.
CREATE TABLE IF NOT EXISTS reservation_history (
    reference      TEXT NOT NULL REFERENCES reservations(reference) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    at             TEXT NOT NULL,
    event          TEXT NOT NULL,
    changes        TEXT NOT NULL,
    revision       INTEGER NOT NULL,
    accepted_terms TEXT NOT NULL,
    PRIMARY KEY (reference, seq)
);
