# Tablekeeper — Stage 3: policies, history and recurring reservations

A containerized HTTP service for restaurant table reservations, and the browser
product in front of it. Diners search availability, book a table — alone or as a
declared pair of tables — receive a confirmation reference, and can cancel or
amend their bookings, including several together in one atomic request.
Restaurants publish dated booking policies, diners can ask *why* a table is not
offered, read a booking's own record, and turn one booking into a recurring
agreement.

Stage 3 is Stage 2 carried forward and widened: every earlier route, error code,
screen and behaviour is unchanged, and no new screen is required — the additions
are the API's. See **[RUN.md](RUN.md)** for the build-and-run command.

```bash
docker build -t tablekeeper . && docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper
curl -s localhost:8080/health      # {"status":"ok"}
```

Implementation: Python 3.12, FastAPI/uvicorn, SQLite (standard library) — no
database server, no outbound network at run time, no manual setup. The screens
are server-rendered as far as the server can see, then driven by one script and
one stylesheet served from the image itself: no CDN, no build step, no framework
fetched at run time.

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
| **Stage 2** — declared pairs, summed capacity, non-transitive | `domain.Restaurant.declares`, `check_combination` | `test_combinations.py` |
| `available_options` alongside unchanged `available_table_ids` | `domain.availability` | `test_combinations.py` |
| `table_ids` on create, amend and moves; `table_id` as a set of one | `parsing.table_set_field`, `service.py` | `test_combinations.py` |
| A booking occupies every table it holds | `repo.confirmed_for_restaurant_in_range`, `reservation_tables` | `test_combinations.py` |
| The four screens, and the controls each one names | `webui.py`, `static/` | `test_screens.py`, `tools/ui-check.mjs` |
| Out-of-order responses, a lost response and its retry | `static/tablekeeper.js` | `tools/ui-check.mjs` |
| A Stage 1 snapshot imports and a signed-in browser stays signed in | `testhooks._upgrade_snapshot` | `test_combinations.py`, `tools/ui-check.mjs` |
| **Stage 3** — published policies: complete, immutable, versioned, manager-only | `policies.py`, `service.publish_policy` | `test_policies.py` |
| A booking's date selects the policy that decides it | `domain.rules_on`, `domain.Rules` | `test_policies.py` |
| `revision` and `accepted_terms` on every reservation response | `domain.reservation_body`, `migrations/0003_stage3.sql` | `test_history.py` |
| `expected_revision` and 409 `stale_revision` | `parsing.expected_revision_field`, `service.py` | `test_history.py` |
| A booking's own record: created, changed, cancelled | `history.py`, `reservation_history` | `test_history.py` |
| `/decision`: the terms a booking currently holds | `service.reservation_decision` | `test_history.py` |
| `explain=true`: both rules, for every table | `domain.availability` | `test_explain.py` |
| Recurring agreements: `POST /series`, `GET /series/{id}` | `service.create_series`, `migrations/0004_series.sql` | `test_series.py` |
| Exceptions and agreement revisions on change, cancel and batch | `service._note_series_change` | `test_series.py` |
| An earlier stage's snapshot imports with terms and a record rebuilt | `testhooks._upgrade_snapshot` | `test_history.py`, `test_series.py` |

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

### Combined tables

A restaurant may declare pairs of tables that can be joined for one party. The
declaration is the only thing that makes a pair bookable: it is never inferred
from capacity, and it is not transitive, so `[t_1,t_2]` and `[t_2,t_3]` do not
make `t_1+t_3` an option. A pair is unordered — asking for `[t_2,t_1]` books the
same seating — and its capacity is the sum of its two tables.

Occupancy stayed one rule and got one extra row per table. A booking's tables
live in `reservation_tables`, one row per member in request order, and the
occupancy query joins through it, so a booking of two tables is reported once
under each of its tables. Nothing above the data layer needs to know whether a
booking holds one table or two: `find_conflict` still answers "is this table
taken in this interval?", and availability, amendments, cancellations and atomic
moves are all the same code they were. `reservations.table_id` stays and holds
the set's first member, which is what keeps a Stage 1 snapshot importable.

`available_table_ids` means exactly what it meant — single tables only — and
`available_options` sits beside it listing every seating the searched party could
take: singles in fixture order, then declared pairs in declaration order with
their tables named in that order. A pair appears only when the two tables
together seat the party and both are free.

### The browser product

Each screen route returns HTML with its controls already in it: the restaurant
select and its options, the date and party size inputs, the search button, the
signup and login fields, the lookup field. What the script draws is the part only
the API knows — the availability matrix, the booking form, the confirmation, a
looked-up booking, and who is signed in.

Three rules from the brief shape the script, and each is a mechanism rather than
a hope:

* **Responses can arrive out of order.** Every request that can change the screen
  takes a sequence number, and a response that is not the newest is dropped
  whole. A slow search cannot restore results over a faster one that followed it.
* **A booking is one intent, and an intent has one idempotency key.** The key is
  bound to the exact body it was made for, so submitting the unchanged form again
  sends the same key and the same body and the service replays the original
  reference. Changing any field is a new intent with a new key.
* **A lost response is not a refusal.** When no response arrives, the diner is
  told the outcome is unknown (`booking-uncertain`) and is never shown an error or
  a confirmation the service did not send. Retrying asks the same question with
  the same key, so a booking that committed before the response was lost comes
  back with its original reference instead of being made twice.

A refusal (`409 table_unavailable`) refreshes the grid and leaves the form and
its inputs exactly as the diner left them, so they can change their choice rather
than start again. Nothing polls, nothing is cached across a reload, and the script
never manufactures a result the service did not return.

Where no browser is installed, `tools/ui-check.mjs` loads these screens into a DOM
and drives the product's own script against a running service, which is how the
three rules above are verified rather than assumed.

### Policies, and the terms a booking accepted

A restaurant's managers publish **complete** policies — every field, never a patch
— and each is immutable and versioned from 1 per restaurant. Policy 0 is the
restaurant's own seeded configuration and is never listed, because it was never
published. A booking's local start date selects the greatest `effective_from` not
later than that date, ties going to the greater version, so publication order and
effective-date order are allowed to differ.

Everything that decides a booking is decided by the selected policy: the slot grid,
the service windows, the sitting length, the capacities (for a pair, their sum) and
the cutoff. The restaurant's detail endpoint keeps reporting the seeded
configuration, as specified — a policy changes what the room does, not what it is.

A reservation carries its `revision` and a snapshot of the whole policy it
accepted, minus the effective date. That snapshot is what makes publication safe:
a new policy never edits a booking already made, a cancellation is judged by the
cutoff the diner actually accepted, and a real amendment checks that cutoff first
and *then* validates every resulting field against the policy for the resulting
date, replacing the terms and the end time together and adding exactly one
revision. A change that changes nothing does none of that. `expected_revision` lets
a client say which revision it meant to change; one that does not match is
`409 stale_revision`, before the booking's state or any field is judged.

### A booking's own record

`GET /reservations/{reference}/history` is the booking's memory, oldest first, with
`seq` increasing by exactly one so the order is total even when two writes land in
the same second. `created` names all three fields from nothing; `changed` names
only what changed, in the order `table_id`, `starts_at_local`, `party_size`;
`cancelled` carries no changes and nothing follows it. Every entry keeps the
revision and the complete terms that resulted from it, so an old entry never
acquires newer terms. A pair is recorded as `table_ids` in the restaurant's
declared order, while the booking itself keeps the order it was asked for — which
is also what makes a reversed pair the same seating rather than an amendment.
`GET /reservations/{reference}/decision` reports the terms the booking holds now,
including after cancellation.

Neither endpoint can be used to discover whether a reference exists: anybody who is
not the owner gets the same 404 as a reference that was never issued, including a
caller who sent no token at all.

### Explaining availability

`explain=true` accounts for **every** table of the restaurant at every slot, in
fixture order, available or not, reporting both rules that decide it — `capacity`
and `no_overlap` — including for a table some other rule already excluded. A rule
that holds is reported holding. `available` is true exactly when both hold, and the
tables whose `available` is true are exactly `available_table_ids` in the same
order. Without the parameter the response keeps its earlier shape.

### Recurring agreements

`POST /series` adopts an existing booking as occurrence zero and generates the
rest: occurrence *i* is the anchor's local calendar date plus *i* × interval × 7
days at the same local clock time, with the anchor's party size and tables. The
anchor itself is untouched — reference, identity, revision, terms, record,
timestamps and its original idempotent response all stay as they were.

Generated occurrences are ordinary bookings, which is why the operation is
all-or-nothing: each one selects the policy for its own date and obeys the ordinary
opening, daylight-saving and occupancy rules, so a date where the local time does
not exist refuses the whole adoption, and so does the first date that cannot be
seated. Nothing partial survives a refusal — no bookings, no records, no counters,
and no claim on the idempotency key.

A diner's own amendment of one occurrence marks it an exception permanently and
counts once against the agreement; a cancellation counts once but is not an
exception, and cancelling the anchor leaves its siblings standing. A batch of moves
counts once per affected agreement, whichever occurrences in it changed. Replays
return the original agreement however much has changed since.

### The restaurant's own revision

Each restaurant counts its successful writes: one for a booking, a real amendment,
a cancellation or a publication, one for a whole batch of moves and one for a whole
adoption, and none for a no-op, a failure or a replay. It starts at 0 after a reset.
No stage-3 response carries it; it is the counter a later stage's seating plans are
judged against, and it is visible in an exported snapshot.

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
  webui.py              the four screen routes, rendered server-side
  static/               tablekeeper.css, tablekeeper.js — served by this image
  policies.py           what a published policy must contain, and how it reads
  history.py            a booking's own record: what changed, from what, to what
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
  migrations/           0001_core.sql … 0004_series.sql
tests/                  697 tests over HTTP
tools/ui-check.mjs      drives the screens in a DOM against a running service
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
| Where `combination_not_allowed` sits in the order of refusals | after the restaurant and every table are known (404), before the calendar is consulted | A set the restaurant does not offer is a fact about the seating, not about the moment; the diner hears it before being told their time is off the grid |
| A set that repeats a table id, however many ids it names | `422 validation_failed` | A repeated id is not a set at all, so it is refused while the request is parsed — before the rule about pairs is ever asked |
| The order of a pair in a response | the order it was asked for | The declaration is unordered (both orders book the same seating); the response reports the diner's own request rather than silently rewriting it |
| Combination cells in the grid | drawn for every declared pair that can seat the searched party, at every slot, `data-available` false when either table is taken | "Shown when a declared pair is available for the searched party size" decides which pairs appear; the attribute then says whether that pair is free at that hour, exactly like a single cell |
| `confirmation-tables` and `reservation-tables` on a single-table booking | present, naming the one table | "A single-table booking's confirmation is unchanged" reads as the same content, not as a missing element — and the element's own rule is "every table label in the reservation" |
| Clicking an available cell while signed out | `auth-error`, not a navigation to `/login` | The brief allows either; an error keeps the diner's search on screen instead of discarding it |
| What a new search does to an open booking form | clears it | The form describes one sitting of one search, so the next search invalidates it. An uncertain retry is not a search, and survives |
| Where the browser keeps a session | the browser's own storage, holding the token and the display name the service returned | No new endpoint, and it survives an export/import because the service keeps the token: a diner signed in before an upgrade is still signed in after it |
| The date input's starting value | today's date in the service's own calendar | Today may be a day the restaurant does not serve; the grid then says so rather than the input guessing a bookable day |
| `GET /restaurants/{id}` for a restaurant that declares no pairs | `"combinable": []` | One shape for a restaurant whether or not it joins tables, so a client never has to ask whether the field is missing or empty |
| A screen route versus the API's conventions | screens return HTML; every other path, known or not, keeps the JSON error envelope | §3.4's `application/json` convention governs the API, and the four screen routes are not API routes |
| Anything wrong inside a policy body | `422 validation_failed`, including a wrong JSON type | The spec states one rule for the policy as a whole — "invalid policy is 422" — so it is applied to the policy as a whole rather than splitting wrong types out into 400 the way a request body's fields are |
| `explain=` with an empty value | `422 validation_failed` | The parameter was sent, and its only accepted value is `true`; reading it as absent would answer a question nobody asked |
| Where `stale_revision` sits | before the booking's state and before any field is validated | The spec places it before cutoff and validation: a client whose view of the booking is old is told that first, whatever else is wrong |
| A pair written in the other order, on a booking that already holds it | not an amendment, and the stored order is left alone | The declaration is unordered, so the set is what the diner holds; rewriting the order would be a change nobody made |
| The order a pair is recorded in | the restaurant's declared combination order | The ledger names one seating one way, so a reversed request and the original read the same in the record |
| An empty `opening_hours` in a policy | accepted, and it closes the room | As in stage 1, a weekday with no window is closed; the field is required, and an empty list is a value |
| Managers named by a fixture | taken as given, like every other seeded reference | Seed data describes a world rather than being a request the API would have accepted |
| A date an agreement cannot seat | the whole adoption is refused | Occurrences are ordinary bookings, so the ordinary error applies; skipping a date would silently deliver a different agreement from the one asked for |
| `count` | includes the anchor, so `count=2` makes one new booking | The spec says so, and the response carries exactly `count` occurrences |
| History, decision and agreement reads with no token | 404, not 401 | The spec resolves these three against the general 401 rule so that none of them reveals whether an identifier exists; credentials that are offered and wrong stay a 401 |
| The restaurant revision | counted, never reported | Stage 3 names the increments but exposes no reading of it, so the counter is kept where a later stage can judge a seating plan against it
