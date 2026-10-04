-- Payments: what a restaurant charges to hold a table, and every movement of
-- money that follows from it.
--
-- Nothing here changes an existing table. A restaurant with no row in
-- `restaurant_payment_settings` takes no deposits, which is exactly how every
-- restaurant behaved before this migration — so a snapshot taken earlier, and
-- every booking made against it, still reads as itself.
--
-- The model is deliberately the small one that covers the case restaurants
-- actually lose money on: a party that books a table for eight on a Friday and
-- does not turn up.
--
--   * a restaurant publishes a deposit — an amount per seat and the party size
--     it applies from;
--   * a booking of that size is authorized (a hold on the diner's card) as part
--     of the booking request, so a declined card refuses the booking rather than
--     holding a table nobody has paid for;
--   * the hold is **captured** when the party does not turn up, and **released**
--     when they do, or when the diner cancels inside the cutoff;
--   * every one of those movements is a row in `payment_events`, so the money
--     can be explained to both sides afterwards.
--
-- The service holds no card data. `payment_method_id` is the provider's own
-- handle for a card the diner already gave them; `provider_ref` is the
-- provider's handle for the hold. A service that stored card numbers would be a
-- service with a PCI problem it does not need.

CREATE TABLE IF NOT EXISTS restaurant_payment_settings (
    restaurant_id           TEXT PRIMARY KEY REFERENCES restaurants(id) ON DELETE CASCADE,
    currency                TEXT NOT NULL,
    -- Cents per seat. The deposit for a booking is this times the party size.
    deposit_per_seat_cents  INTEGER NOT NULL,
    -- The party size the deposit applies from. A table for two is not worth a
    -- hold; a table for eight on a Friday is.
    deposit_from_party_size INTEGER NOT NULL,
    updated_at              TEXT NOT NULL,
    updated_by              TEXT NOT NULL
);

-- One row per booking that has money attached to it. `reference` is the
-- reservation's own reference: the hold and the booking are the same promise,
-- which is why cancelling the booking releases the hold rather than leaving two
-- things to reconcile.
CREATE TABLE IF NOT EXISTS payment_intents (
    id             TEXT PRIMARY KEY,
    restaurant_id  TEXT NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    reference      TEXT NOT NULL,
    user_id        TEXT NOT NULL,
    provider       TEXT NOT NULL,
    -- The provider's handle for this hold: what a capture or a release names.
    provider_ref   TEXT NOT NULL,
    currency       TEXT NOT NULL,
    amount_cents   INTEGER NOT NULL,
    captured_cents INTEGER NOT NULL DEFAULT 0,
    -- authorized | captured | released | failed
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_payment_intents_reference
    ON payment_intents (reference);
-- Indexed on the columns that are filtered, not on `rowid`: SQLite will not
-- accept its implicit key in an index, and ordering by it needs no index.
CREATE INDEX IF NOT EXISTS idx_payment_intents_restaurant
    ON payment_intents (restaurant_id);

-- The ledger. Append-only: what was authorized, captured or released, when, and
-- for how much. Nothing updates or deletes a row here.
CREATE TABLE IF NOT EXISTS payment_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id    TEXT NOT NULL REFERENCES payment_intents(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    detail       TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_payment_events_intent ON payment_events (intent_id, id);

-- Attempts that never became a hold: a card the provider refused, or a diner
-- who tried to book without one. Kept because "how often do cards fail on a
-- Friday" is a question a restaurant asks, and because the booking those
-- attempts were for does not exist to record them.
CREATE TABLE IF NOT EXISTS payment_attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id TEXT NOT NULL,
    reference     TEXT,
    user_id       TEXT NOT NULL,
    amount_cents  INTEGER NOT NULL,
    outcome       TEXT NOT NULL,
    reason        TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_payment_attempts_restaurant
    ON payment_attempts (restaurant_id, id);

-- Account recovery: a reset link, and proof that an address is real.
--
-- Both store a SHA-256 of the token rather than the token: a database that leaks
-- does not hand anybody a working link, which is the same reason passwords are
-- hashed. Both are single-use and both expire.
--
-- `email_verified` is a separate table rather than a column on `users`, so a
-- snapshot taken before verification existed imports unchanged and reads as
-- itself — an account with no row is simply unverified, which is what it was.
CREATE TABLE IF NOT EXISTS password_resets (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_password_resets_user ON password_resets (user_id);

CREATE TABLE IF NOT EXISTS email_verifications (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_email_verifications_user
    ON email_verifications (user_id);

CREATE TABLE IF NOT EXISTS email_verified (
    user_id     TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    verified_at TEXT NOT NULL
);
