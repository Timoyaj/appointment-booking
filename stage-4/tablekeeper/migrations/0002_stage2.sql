-- Stage 2: declared table combinations, and the tables each reservation holds.
--
-- A combination is a pair the restaurant declared. Declaration order is kept
-- because the API lists pairs in `combinable` order and names the pair's tables
-- in that order.
--
-- A reservation's tables get their own table, one row per member in request
-- order, so occupancy can be judged one table at a time without any caller
-- knowing how many tables a reservation holds. `reservations.table_id` stays and
-- holds the set's first member: it keeps a Stage 1 snapshot importable and gives
-- the row a single-table identity for the responses that still carry `table_id`.

CREATE TABLE IF NOT EXISTS restaurant_combinable (
    restaurant_id TEXT NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    position      INTEGER NOT NULL,
    table_a       TEXT NOT NULL,
    table_b       TEXT NOT NULL,
    PRIMARY KEY (restaurant_id, position)
);

CREATE TABLE IF NOT EXISTS reservation_tables (
    reference TEXT NOT NULL REFERENCES reservations(reference) ON DELETE CASCADE,
    position  INTEGER NOT NULL,
    table_id  TEXT NOT NULL,
    PRIMARY KEY (reference, position)
);

CREATE INDEX IF NOT EXISTS idx_reservation_tables_table ON reservation_tables (table_id);
