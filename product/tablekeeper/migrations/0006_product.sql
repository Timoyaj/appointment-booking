-- Product layer: sessions that expire, staff and roles, login throttling, the
-- notification outbox, and the audit trail of what a manager changed.
--
-- Every table here is new and no existing table gains a column, deliberately.
-- A snapshot exported before this migration therefore imports unchanged -- the
-- tables it never heard of are simply absent, and an absent table imports empty
-- -- and a row that *is* here but predates the feature reads as itself:
--
--   * a token with no `token_sessions` row is a token issued before sessions
--     existed. It never expires and is never revoked, which is exactly how it
--     behaved when it was issued, so an upgraded service does not sign anybody
--     out by upgrading.
--   * a `restaurant_managers` row with no `restaurant_staff` row is a manager
--     as the older stage understood one: a manager, and nothing else.
--
-- Notifications are rows before they are messages. A booking writes its
-- confirmation inside the same transaction that writes the booking, so the
-- message cannot be lost by a crash between the two and cannot exist for a
-- booking that rolled back. Sending is a separate, retryable step that reads
-- these rows, which is what keeps the booking path free of outbound network.

-- Sessions that expire ------------------------------------------------------ #
-- A side table rather than new columns on `tokens`: the token store keeps its
-- exact shape, and "expires at" is knowledge a token gains after it is issued
-- and loses when it is revoked.
CREATE TABLE IF NOT EXISTS token_sessions (
    token      TEXT PRIMARY KEY REFERENCES tokens(token) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_token_sessions_expiry ON token_sessions (expires_at);

-- Staff, roles and the audit trail ------------------------------------------ #
-- `restaurant_managers` stays authoritative for "may publish a policy or plan
-- the seating", so every rule the earlier stages wrote still reads the table it
-- was written against. `restaurant_staff` is the richer layer a product needs:
-- it records *how* each person is attached to the restaurant, and the creator of
-- a restaurant is attached as its owner.
CREATE TABLE IF NOT EXISTS restaurant_staff (
    restaurant_id TEXT NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (restaurant_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_staff_user ON restaurant_staff (user_id);

-- Who did what, when, and to which restaurant. Manager-only writes are already
-- refused to everybody else; this is the record of which manager made the call,
-- which is the first thing an owner asks when a plan moves their bookings.
CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    action        TEXT NOT NULL,
    detail        TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_restaurant ON audit_log (restaurant_id, id);

-- Login throttling ----------------------------------------------------------- #
-- One row per login attempt. Failures are counted over a window, so a locked-out
-- account recovers by itself instead of needing an operator, and a success
-- clears the count for that address.
CREATE TABLE IF NOT EXISTS login_attempts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    email     TEXT NOT NULL,
    at        TEXT NOT NULL,
    succeeded INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_login_attempts_email ON login_attempts (email, id);

-- The notification outbox ---------------------------------------------------- #
-- `status` is one of queued, sent, failed. `attempts` counts deliveries tried,
-- so a transport that is down is visible rather than silent, and a message that
-- keeps failing stops being retried forever.
CREATE TABLE IF NOT EXISTS notifications (
    id            TEXT PRIMARY KEY,
    restaurant_id TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    reference     TEXT,
    kind          TEXT NOT NULL,
    to_email      TEXT NOT NULL,
    subject       TEXT NOT NULL,
    body          TEXT NOT NULL,
    status        TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at    TEXT NOT NULL,
    sent_at       TEXT
);

-- Messages are read and drained in `rowid` order — insertion order — rather than
-- by `id`, which is a random string: ordering by that would shuffle a diner's
-- messages and could deliver a cancellation before the confirmation it cancels.
-- `rowid` is the table's implicit key, so it needs no index of its own; these
-- cover the filters that go with it.
CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications (status);
CREATE INDEX IF NOT EXISTS idx_notifications_restaurant
    ON notifications (restaurant_id);
