# Tablekeeper — the product layer

This folder is `stage-4/` carried forward the way every earlier stage was carried
forward: the whole reservation engine, unchanged and still passing its 887 tests,
plus what a restaurant needs before it can be asked to **pay** for a booking
system. The assessment that chose this work is in
[../SELLING.md](../SELLING.md); this file is what was built.

```bash
docker compose up           # or: docker build -t tablekeeper . && \
                            #     docker run --rm -p 8080:8080 -v tablekeeper-data:/data tablekeeper
open http://localhost:8080/console
```

Everything the engine did, it still does, byte for byte: same routes, same error
codes, same screens, same export format. **1026 tests pass** — the 887 it arrived
with, plus 139 for the product layer: sessions, onboarding, roles, the outbox and
audit trail (53), the two new screens (16), deposits (30), reporting (22), account
recovery (14) and the snapshot round-trip for the new tables (4).

---

## What was added

### 1. The service is safe to expose

| Before | Now |
| --- | --- |
| `/_test/reset`, `/_test/export` and `/_test/import` were unauthenticated **and enabled in the shipped image**. One `curl -X POST …/_test/reset` wiped every reservation. | `TABLEKEEPER_TEST_HOOKS=0` in the image means those routes are **not registered at all** — not refused, absent. The reference checks still run against a container started with the variable set to `1`. |
| Tokens never expired and could not be revoked. A token seen over a shoulder worked forever. | A token gets a **session** with an expiry (30 days by default, `TABLEKEEPER_TOKEN_TTL_MINUTES`). `POST /auth/logout` ends one device, `POST /auth/logout-all` ends every one. `GET /auth/session` says who you are and when your token stops working. |
| Nothing stopped a machine guessing passwords at full speed. | Failures are counted per address: **8 in 15 minutes locks that address out** for the window, answered `429 too_many_attempts`. The counter is written *after* the refusing transaction rolls back — otherwise the failure would be undone by the very rollback it describes. |
| A snapshot from an older stage imported and signed every diner out. | Tokens that predate sessions have no session row and **never expire**, exactly as they behaved when issued. Importing an older export does not log anybody out. |

### 2. A restaurant can be created by a person, not a fixture

Until now the only caller of `repo.insert_restaurant` was the test hook: a
restaurant could exist only by having a fixture POSTed at it. Now:

- **`POST /restaurants`** — an authenticated person describes their room (name,
  IANA timezone, slot grid, sitting length, cancellation cutoff, opening hours,
  tables, and which tables join) and gets it, with themselves as its **owner**.
  Idempotent under an optional `Idempotency-Key`, so a retry over a flaky
  connection does not open two restaurants.
- **`GET /restaurants/mine`** — the restaurants you own, manage or host.
- **Roles: owner / manager / host.** `POST`, `PATCH` and `DELETE` on
  `/restaurants/{id}/staff` add people who already have accounts, change what
  they may do, and remove them. Owners decide who works there; a manager may
  publish policies and plan seating; a host may see the room and change none of
  its rules. The last owner cannot be demoted or removed.
- The permission rules the earlier stages wrote still read `restaurant_managers`,
  so **nothing about who may publish a policy or apply a plan changed** — the new
  table is kept in step with the old one rather than replacing it.
- **`GET /restaurants/{id}/audit`** — who did what here, and when. Manager-only
  writes were already refused to everybody else; this is the record of *which*
  manager made the call.

### 3. Diners are actually told things

There was not one occurrence of `smtp`, `send` or `notification` in the codebase.
A diner booked a table and heard nothing.

Messages are now written as **rows in an outbox, inside the same transaction as
the change they describe**. That ordering is the whole design:

- a confirmation cannot exist for a booking that rolled back;
- a booking cannot exist without its confirmation waiting to go out;
- a retried request sends **one** message, because the idempotent replay returns
  the stored response before any of it runs.

Written on every real change: confirmed, cancelled, amended, moved, reseated by
an applied plan (`seating_changed`), and adopted into a recurring agreement.

Sending is a separate, retryable step. `POST /restaurants/{id}/notifications/drain`
hands queued messages to the configured transport; with **no transport
configured it reports what is still queued instead of marking anything sent** —
an unconfigured deployment is visibly unconfigured. `SmtpTransport.from_env`
reads `TABLEKEEPER_SMTP_*`; a transport that raises marks that one message
`failed` with the error and moves on, and `POST …/notifications/{id}/retry` puts
it back. `GET /restaurants/{id}/notifications` is the outbox a manager can read.

Ordering is by `rowid`, not by id. Ids are random strings; a test caught that
ordering by them could deliver a cancellation **before** the confirmation it
cancels.

### 4. Screens for the people who run the restaurant

Every manager action since stage 3 was reachable only by composing JSON against
the API. That is not something a restaurant manager can be asked to do.

- **`/start`** — the onboarding form: name, timezone, service and sitting
  lengths, cutoff, opening hours, tables, and pairs that join.
- **`/console`** — pick a restaurant you work at and see the room, the tables and
  which join, the staff and their roles, **who is coming** this week with buttons
  to record a no-show or mark the party arrived, **the deposit you take** (drawn
  from `/restaurants/{id}/payment-settings`, with a form to publish one and a
  button to stop), **this month's numbers** (covers, utilisation, deposits kept,
  cards refused), the outbox (sent / waiting / failed) with a button to deliver
  what is waiting, and the audit trail.

Both are server-rendered as far as the server can see and driven by
`static/console.js`, which talks to the **same public API** a diner's browser
does. The console is not a privileged back door: every rule about who may do what
is enforced in the service, in one place, rather than duplicated in a template.

### 5. Deposits, so a no-show costs something

A restaurant that wants money for a table can now ask for it:

- **`PUT /restaurants/{id}/payment-settings`** — a manager publishes a deposit:
  currency, cents per seat, and the party size it starts at. **`DELETE`** stops
  it; holds already taken are untouched. Both are audited
  (`deposit_published`, `deposits_stopped`).
- A booking that owes a deposit must name a `payment_method_id`. Without one it is
  refused **402 `payment_required`**, in words: *"a deposit of 40.00 EUR is
  required for a party of 4"*. With one, the hold is taken **inside the booking
  transaction**: a declined card leaves no booking behind, and the refusal is
  recorded in a table of its own, outside the rollback that describes it.
- **`POST /reservations/{ref}/no-show`** captures what the terms said it would;
  **`POST /reservations/{ref}/complete`** releases it and leaves the booking live;
  cancelling releases it. **`GET /reservations/{ref}/payments`** is the ledger —
  append-only events (`authorized`, `captured`, `released`), readable by the diner
  or by staff.
- Card numbers are **never** stored, and no card data reaches this service:
  `TABLEKEEPER_STRIPE_SECRET_KEY` selects a real provider, and with no key
  configured the in-process `FakeProvider` takes and releases holds so the whole
  path is exercisable end to end. A provider that will not release leaves the
  intent `authorized` rather than claiming otherwise.

### 6. Reporting: the numbers a restaurant is run by

**`GET /restaurants/{id}/reports/summary?from=&to=`** answers, over a window of
the restaurant's own calendar (≤366 days): bookings split confirmed / cancelled /
no-show, covers served and **utilisation** against the hours actually published
for each day in the window, covers by hour and by day, party-size buckets, lead
time, **table use** (which tables earn their floor space — a party seated across
two counts for both), no-show and cancellation rates, and money: deposits kept,
card attempts, refusals. **`GET …/reports/bookings.csv`** is the same window as a
spreadsheet, with cells that begin `=`, `+`, `-` or `@` prefixed so a guest cannot
turn a name into a formula.

Revenue is *money captured*, never a guess at menu spend. A window with no service
reports utilisation `0.0` rather than dividing by zero.

### 7. Getting back into an account

- **`POST /auth/password-reset`** always answers **202**, with the same body
  whether or not the address has an account here: an endpoint that says "no such
  user" is an endpoint for finding out who a restaurant's guests are. If it does
  exist, a single-use link is queued and mailed (60 minutes by default,
  `TABLEKEEPER_RESET_TTL_MINUTES`).
- **`POST /auth/password-reset/confirm`** changes the password and **signs every
  device out** — `sessions_revoked` says how many. Asking for another link
  retires the last one.
- **Signing up sends a confirmation link** (`POST /auth/verify-email` to confirm,
  `/auth/verify-email/resend` to ask again); `GET /auth/session` reports
  `email_verified`. `TABLEKEEPER_REQUIRE_VERIFIED_EMAIL=1` makes the service
  refuse to seat a diner whose address is unconfirmed (403 `email_not_verified`);
  it is **off** by default, because a mistyped address must not lock a paying
  diner out of a restaurant they can walk into.
- Tokens are 32 random bytes and are stored as **SHA-256 hashes**, never in the
  clear; they are single-use and expiring. A credential message is scrubbed from
  the outbox once the transport has taken it, so a snapshot taken afterwards
  cannot be replayed into somebody's account.

---

## What is still not here

Deliberately, and in the order the assessment ranked them:

1. **Email leaves the building only if you configure SMTP.** The outbox, the
   retries and the copy are real; the transport is off until a deployment turns
   it on. Nothing has been sent to a real inbox from this repository, and the
   reset and confirmation emails are exercised through the recorder.
2. **Nothing is charged unless a Stripe key is configured**, and the Stripe path
   is tested against a stubbed HTTP endpoint rather than a live account. With no
   key the in-process provider is used, which proves the flow and not the vendor.
3. **SQLite in a volume, single tenant.** Fine for one restaurant or twenty;
   Postgres and tenant isolation in the data layer come before customer two.
4. **No reminders.** A 24-hour reminder is the cheapest no-show reduction there
   is, and it needs a scheduler rather than a request — the outbox is the seam it
   would use.
5. **Nothing lets a diner change or cancel from the link in their email**, and
   there is no waitlist to fill a table when somebody cancels. Both are the next
   tier after this one.
6. **The console reads more than it writes.** Publishing a deposit and no-show /
   complete are on the page now; publishing a policy and previewing or applying a
   seating plan are still API-only, and the page says so.
7. **No rate limit on reservation creation itself**, and no PII/data-retention
   policy or LICENSE — the two things that have to exist before a real customer's
   data is in here.

---

## How this was verified

Three layers, all run against a live service:

```bash
cd product
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q                     # 1026 passed

# the screens, driven in a DOM against a running service
cd tools && npm install
npm run check:diner     # 25 passed, 0 failed — the four diner screens
npm run check:console   # 11 passed, 0 failed — the console and onboarding
npm run check           # both
```

The 887 tests that came from `stage-4/` pass unchanged, which is the evidence
that the product layer is additive: it adds tables and routes and changes no
answer the earlier stages gave. The new tests are `tests/test_product.py`
(sessions, throttling, onboarding, roles, the outbox, the audit trail),
`tests/test_console_screens.py` (the two new screens), `tests/test_payments.py`,
`tests/test_reports.py`, `tests/test_recovery.py` and
`tests/test_product_state.py` (a snapshot round-trip that carries deposits, the
ledger and a live reset link across).

`console-check.mjs` is the one worth pointing at: it loads `/console` and
`/start` into a DOM, gives the page a real `fetch`, evaluates `static/console.js`
against the running service, and asserts the dashboard draws the room, its staff
and its outbox, that tonight's list is drawn and a no-show really is recorded,
that a manager can publish a deposit and stop it again from the form, that
"this month" is on the page with its covers and utilisation, that a stranger is
told they work nowhere, that a signed-out visitor is asked to sign in and
**cannot** create a restaurant from the form, and that submitting the onboarding
form really opens one.

It also found two bugs that no HTTP-level test would have: the session key in
`console.js` did not match the diner script's (so signing in on `/login` left the
console signed out), and the script booted only on `DOMContentLoaded`, which had
already fired by the time a deferred script runs.

### Known gaps in the verification

- **The container has never been built here** — there is no Docker daemon in this
  workspace, so `Dockerfile`, `docker-compose.yml` and the `TABLEKEEPER_TEST_HOOKS=0`
  default are verified by reading, not by building.
- **No message has been delivered over SMTP to a real server.** The outbox, the
  retry and failure states and the transport contract are tested; the socket is not.
- **Layout and paint are not checked.** jsdom sees the DOM, not the pixels.
