# Tablekeeper — Stage 1: reservations

A containerized HTTP service for restaurant table reservations. Diners search
availability, book a table, receive a confirmation reference, and can cancel or
amend their bookings — including changing several bookings together in one
atomic request.

Built to the Stage 1 brief: only the HTTP API, delivered as a self-contained
container. See **[RUN.md](RUN.md)** for the build-and-run command.

```bash
docker build -t tablekeeper . && docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper
curl -s localhost:8080/health      # {"status":"ok"}
```

Implementation: Python 3.12, FastAPI/uvicorn, SQLite (standard library) — no
database server, no outbound network at run time, no manual setup.

---

## What is implemented

| Brief | Where | Tests |
| --- | --- | --- |
| §2 Container, `PORT`, no runtime network | `Dockerfile`, `tablekeeper/serve.py` | `RUN.md` smoke test |
| §3.2 `GET /health` | `api.py` | `test_health_reset.py` |
| §3.3 `POST /_test/reset` | `testhooks.py` | `test_health_reset.py` |
| §3.4 Conventions (JSON, RFC 3339, ignored unknowns, 64-char ids) | `parsing.py`, `tztime.py` | throughout |
| §5 Error envelope and codes | `errors.py`, `api.py` handlers | `test_errors` cases across all suites |
| §6 Signup, login, bearer tokens, hashed passwords | `auth.py` | `test_auth.py` |
| §7 Idempotency | `idempotency.py` | `test_idempotency.py` |
| §8 Restaurants, availability, reservations, cancel, amend | `service.py`, `domain.py` | `test_availability.py`, `test_reservations.py`, `test_cancel_amend.py` |
| §9 Time and DST | `tztime.py` | `test_dst.py` |
| §10 Export / import | `testhooks.py`, `repo.dump_state` | `test_export_import.py` |
| §11 Atomic reservation moves | `service.move_reservations` | `test_moves.py` |
| No 5xx under load, up to 50 in flight | `db.py`, `api.py` thread pool | `test_concurrency.py` |

299 tests, all talking to the service over HTTP through the ASGI app.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

---

## Design notes

### Occupancy and atomicity

Two `confirmed` reservations may never occupy the same table at overlapping
times. Occupancy is the half-open interval `[starts_at, starts_at +
reservation_duration)` in **absolute** time, so a 90-minute booking at 19:00
does not overlap one at 20:30.

Every write runs inside a `BEGIN IMMEDIATE` SQLite transaction
(`tablekeeper/db.py`), which takes the write lock *before* the first statement
executes. Concurrent bookings therefore serialise instead of both reading "table
free" and both writing: exactly one wins and the others get `409
table_unavailable`. Connections are thread-local with WAL enabled, so the HTTP
thread pool can read while a writer holds the lock. `test_concurrency.py` races
50 requests for one slot and asserts one 201, forty-nine 409s and no 5xx; the
same race run against the container with 50 parallel clients gives the same
result.

Because `starts_at`/`ends_at` are stored with the restaurant's local offset,
their string order is not chronological across a DST change. Reservations
therefore also keep `starts_at_utc`/`ends_at_utc`, which are always UTC and
fixed-width, so range queries and `starts_at`-descending ordering stay correct.

### One rule set

`domain.resolve_sitting` is the only place that decides whether a local start
time is bookable: does the local time exist (§9), does the sitting fit inside a
published service window, and is it aligned to the `slot_minutes` grid. The same
function backs `GET /availability`, `POST /reservations`,
`PATCH /reservations/{reference}` and `POST /reservation-moves`, so a slot the
grid offers can always actually be booked and one it does not offer is always
rejected — `test_availability.py` asserts exactly that.

Validation order for a placement is: unknown restaurant/table (`404`) → local
time does not exist (`422 invalid_local_time`) → outside opening hours
(`422 outside_opening_hours`) → off grid (`422 not_on_slot_grid`) → party does
not fit (`422 party_exceeds_capacity`) → table taken (`409 table_unavailable`).

### Idempotency

`POST /reservations` and `POST /reservation-moves` require an
`Idempotency-Key`, scoped to the authenticated user and to the method and path.
The key lookup and the write share one transaction, so for concurrent identical
requests exactly one returns 201 and the others return 200 with the same body.
The completed request body, the status and the response are stored, so a replay
returns the original response even after the reservation is amended or
cancelled, and export/import carries receipts across. Failed (4xx) requests are
deliberately *not* recorded, so a key is not burned by losing a race.

### Batch moves

`POST /reservation-moves` validates the whole set against the state the set
would produce — bookings outside the batch as stored, bookings inside it at
their new placements. That is what makes a two-booking swap possible while still
refusing a genuine clash, and why a listed-but-unchanged booking still occupies
its table. Errors are resolved in the order the brief specifies: shape (`422`),
ownership (`404`), one restaurant (`422`), then per booking in input order —
cancelled (`409`), cutoff (`409`) and only then its other field errors — and
occupancy clashes (`409 table_unavailable`) last. Either every move commits or
nothing changes: occupancy, reservation records and retry keys alike.

### Time and DST

`starts_at_local` is a bare wall-clock `YYYY-MM-DDTHH:MM` resolved against the
restaurant's IANA zone (`tztime.resolve_local`):

* a time inside a spring-forward gap does not exist → `422 invalid_local_time`,
  and it never appears in availability;
* a time inside a fall-back fold occurs twice and always resolves to the **first**
  occurrence, before the clocks change; the slot appears once in availability;
* `reservation_duration_minutes` is absolute, so `ends_at` is the start instant
  plus the duration rendered back into local time — a 90-minute booking from
  01:30 on a fall-back night ends at local 02:00, not 03:00.

The IANA database ships in the image via the `tzdata` package, so the 2026
transitions for `Europe/Berlin` and `America/New_York` are correct even on a slim
base image with no system zoneinfo. `test_dst.py` covers both zones and both
directions.

### Errors

Every 4xx and 5xx carries `{"error": {"code", "message"}}`, including routing
failures and anything unexpected: handlers exist for `ApiError`, Starlette HTTP
exceptions, request-validation errors and bare `Exception`. A field of the wrong
JSON type is `400 malformed_request`; a missing required field is
`422 validation_failed`; a right-typed but invalid value is `422`, with the
endpoint-specific codes (`party_size` strings and booleans, `starts_at_local`
with an offset) taking precedence as specified. Integer query parameters must be
plain decimal digits, so `1e9`, `4.0` and `+4` are `422`.

### Authentication

Passwords are hashed with scrypt (`hashlib`, ~50 ms per hash) and stored as
`scrypt$n$r$p$salt$hash` — never in plaintext. Tokens are opaque random strings
that do not expire, and an account may hold many of them at once. Unknown-email
and wrong-password logins are indistinguishable (`401 unauthenticated`), as are
another diner's reservation and one that does not exist (`404 not_found`), so
existence is never leaked. `GET /restaurants`, `GET /restaurants/{id}` and
`GET /availability` are public; `/health` and the three `/_test/*` endpoints need
no token.

---

## Project layout

```
Dockerfile              self-contained image, runs unprivileged, listens on $PORT
RUN.md                  build-and-start command, smoke test, configuration
requirements.txt        runtime dependencies (installed at build time)
requirements-dev.txt    test dependencies (not in the image)
tablekeeper/
  serve.py              entry point: 0.0.0.0:$PORT, default 8080
  api.py                routes, plumbing and every error handler
  service.py            book / list / read / cancel / amend / move
  domain.py             the rule set: grid, hours, DST, capacity, occupancy
  testhooks.py          reset, export, import
  auth.py               scrypt hashing, signup, login, bearer tokens
  idempotency.py        per-user, per-path execute-once receipts
  parsing.py            §5's type/missing/format error semantics
  tztime.py             local <-> UTC, gap and fold resolution, RFC 3339
  repo.py               all SQL, plus state dump/replace for export/import
  db.py                 SQLite engine: thread-local, re-entrant BEGIN IMMEDIATE
  clock.py              injectable clock so tests never sleep
  errors.py             status + code for every rejection
  migrations/           0001_core.sql
tests/                  299 tests over HTTP
```

Interactive API docs (`/docs`) are intentionally disabled: Swagger UI loads its
scripts and stylesheets from a CDN, and the runtime has no outbound network, so
the page would render blank. The API is documented in `RUN.md` and this file.

State is ephemeral and lives in the container filesystem
(`/tmp/tablekeeper/tablekeeper.db`); it need not survive a restart.

---

## Where the spec leaves room to choose

Recorded so a reader can see the decision rather than rediscover it. Each of these is
a reading of the brief, not something it states outright:

| Situation | Choice | Why |
| --- | --- | --- |
| A start time that is both outside opening hours and off the slot grid | `422 outside_opening_hours` | §8 phrases the grid rule as "**slot** outside opening hours", so hours are decided first and the grid applies to times that are inside service |
| Same idempotency key, same fields, one extra unknown field | `409 idempotency_key_reuse` | §7 defines "same body" as the same JSON value, and names only key order and whitespace as things that do not matter |
| `null` for an optional `PATCH`/move field | treated as omitted | The field is optional and §3.4 says unknown fields are ignored; a null carries no change to make |
| Two emails differing only in case | the same account | Uniqueness and login are case-insensitive (`COLLATE NOCASE`); the original spelling is preserved in responses |
| Structural problems inside `moves` (not a list, 9 items, duplicate or non-string reference) | `422 validation_failed` | §11's "invalid shape … gives 422" is an endpoint-specific rule and takes precedence over the generic wrong-type 400 |
| `POST /reservations/{reference}/cancel` body | optional; if present it must be a JSON object | §8 documents no body for cancel, but a malformed one is still a client error |
| Seeded reservation `reference` in a fixture | must match the §8 format, 6–12 of `A-Z0-9` | §8 states the format for references, and §4 seeds reservations carrying a `reference`; one format for every reference in the system |
| Unspecified HTTP status (e.g. 405 on a known path) | the error envelope, code `not_found` | §5 requires every 4xx/5xx to carry the envelope; the status is passed through unchanged |
| Interactive API docs (`/docs`) | disabled | Swagger UI loads its scripts and stylesheets from a CDN and §2 requires runtime assets to be in the image, with no outbound network at run time |
