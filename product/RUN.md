# RUN.md

## One command: build and start

```bash
docker build -t tablekeeper . && docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper
```

That is the whole setup. The image is self-contained: Python, FastAPI/uvicorn,
the IANA timezone database, the application code, the stylesheet and the browser
script are all installed at build time, and nothing is fetched when the container
runs — no CDN, no font, no package registry. The service listens on
`0.0.0.0:$PORT` (default `8080`) and creates its SQLite database at
`/tmp/tablekeeper/tablekeeper.db` on first start — no volumes, no environment
setup, no external database.

On a different port:

```bash
docker build -t tablekeeper . && docker run --rm -p 9000:9000 -e PORT=9000 tablekeeper
```

## Verify it is up

```bash
curl -s localhost:8080/health
# {"status":"ok"}
```

`GET /health` returns 200 as soon as the service and its data store can serve
requests, well inside the 60-second budget (startup is a schema migration of an
empty SQLite file, typically under a second).

The API and the browser product are one process on one port. The five screens are
reachable by URL and return HTML:

| Route | Screen |
| --- | --- |
| `http://localhost:8080/` | Search and the availability grid |
| `http://localhost:8080/signup` | Create an account |
| `http://localhost:8080/login` | Sign in |
| `http://localhost:8080/lookup` | Look up a booking by reference |
| `http://localhost:8080/bookings` | The signed-in diner's own reservations |

```bash
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' localhost:8080/
# 200 text/html; charset=utf-8
```

## A complete smoke test

Load a fixture with two combinable tables, sign in, search, book a pair, look it
up and cancel it:

```bash
B=localhost:8080

# 1. Load the fixture (replaces all state; repeatable)
curl -s -o /dev/null -w '%{http_code}\n' -X POST $B/_test/reset \
  -H 'Content-Type: application/json' -d '{
    "users": [{"id":"u_ada","email":"ada@example.com","password":"correct horse","display_name":"Ada"}],
    "restaurants": [{
      "id": "r_anker", "name": "Zum Anker", "timezone": "Europe/Berlin",
      "slot_minutes": 30, "reservation_duration_minutes": 90,
      "cancellation_cutoff_minutes": 120,
      "opening_hours": [{"weekday":"thu","opens":"18:00","closes":"23:00"}],
      "tables": [{"id":"t_1","label":"1","capacity":2},{"id":"t_2","label":"2","capacity":4},
                 {"id":"t_3","label":"3","capacity":6}],
      "combinable": [["t_1","t_2"],["t_2","t_3"]],
      "manager_user_ids": ["u_ada"]
    }],
    "reservations": []}'
# 204

# 2. Log in as the seeded user
TOKEN=$(curl -s -X POST $B/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"ada@example.com","password":"correct horse"}' | sed 's/.*"token":"\([^"]*\)".*/\1/')

# 3. Search (public, no token). Every slot lists single tables and the declared
#    pairs that can seat the party: available_options.
curl -s "$B/availability?restaurant_id=r_anker&date=2026-10-15&party_size=7"
# available_table_ids is empty — no single table seats seven —
# and available_options offers [{"table_ids":["t_2","t_3"],"capacity":10}]

# 4. Book the pair, idempotently
curl -s -X POST $B/reservations -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-1' -d '{
    "restaurant_id":"r_anker","table_ids":["t_2","t_3"],
    "starts_at_local":"2026-10-15T19:00","party_size":7}'
# 201, with "table_ids":["t_2","t_3"] and no "table_id", and a reference such as "K3P7QW9X"

# 5. Retry the identical request: 200 with the same body, no second booking
curl -s -o /dev/null -w '%{http_code}\n' -X POST $B/reservations \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-1' -d '{
    "restaurant_id":"r_anker","table_ids":["t_2","t_3"],
    "starts_at_local":"2026-10-15T19:00","party_size":7}'
# 200

# 6. Both tables are held for the whole sitting
curl -s -X POST $B/reservations -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-2' -d '{
    "restaurant_id":"r_anker","table_id":"t_3",
    "starts_at_local":"2026-10-15T19:30","party_size":4}'
# 409 {"error":{"code":"table_unavailable",...}}

# 7. An undeclared pair is refused
curl -s -X POST $B/reservations -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-3' -d '{
    "restaurant_id":"r_anker","table_ids":["t_1","t_3"],
    "starts_at_local":"2026-10-15T21:00","party_size":4}'
# 422 {"error":{"code":"combination_not_allowed",...}}

# 8. Look the booking up and cancel it (replace REFERENCE with the one from step 4)
curl -s $B/reservations/REFERENCE -H "Authorization: Bearer $TOKEN"
curl -s -X POST $B/reservations/REFERENCE/cancel -H "Authorization: Bearer $TOKEN"
# "status":"cancelled", and both tables are free again
```

## Stage 3 in the same smoke test

Policies, explanations, a booking's own record, and a recurring agreement. These
continue from the fixture and `$TOKEN` above, and need one booking of their own:

```bash
# 9. Ask why a table is not offered: every table is accounted for, both rules each
curl -s "$B/availability?restaurant_id=r_anker&date=2026-10-15&party_size=7&explain=true"
# each slot carries "explain": [{"table_id","policy_version","available",
#   "rules":[{"rule":"capacity","holds":..},{"rule":"no_overlap","holds":..}]}, ...]

# 10. Publish a policy, as a manager, idempotently
curl -s -X POST $B/restaurants/r_anker/policies -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: policy-1' -d '{
    "effective_from": "2026-11-01", "slot_minutes": 30,
    "reservation_duration_minutes": 60, "cancellation_cutoff_minutes": 60,
    "opening_hours": [{"weekday":"thu","opens":"18:00","closes":"23:00"}],
    "capacities": {"t_1":2,"t_2":4,"t_3":6}}'
# 201, with the policy as supplied plus "policy_version": 1
# A diner who is not a manager gets 403; a second publication gets version 2.

curl -s $B/restaurants/r_anker/policies
# {"policies":[...]} in publication order, and never policy 0

# 11. Book under the seeded rules, then read what it accepted
REF=$(curl -s -X POST $B/reservations -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-9' -d '{
    "restaurant_id":"r_anker","table_id":"t_2",
    "starts_at_local":"2026-10-15T19:00","party_size":4}' | sed 's/.*"reference":"\([^"]*\)".*/\1/')
curl -s $B/reservations/$REF/decision -H "Authorization: Bearer $TOKEN"
# {"reference":...,"revision":1,"accepted_terms":{"policy_version":0,...}}

# 12. Change it, and read the booking's own record
curl -s -X PATCH $B/reservations/$REF -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"party_size":3}'
curl -s $B/reservations/$REF/history -H "Authorization: Bearer $TOKEN"
# entries: created (all three fields from null), then changed (party_size 4 -> 3)

# A revision the booking no longer has is refused before anything else is judged
curl -s -X PATCH $B/reservations/$REF -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"party_size":2,"expected_revision":1}'
# 409 {"error":{"code":"stale_revision",...}}

# 13. Adopt it as a recurring agreement: four Thursdays a week apart
curl -s -X POST $B/series -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: series-1' \
  -d "{\"anchor_reference\":\"$REF\",\"count\":4,\"interval_weeks\":1}"
# 201 {"series_id":"ser_...","revision":1,"interval_weeks":1,"occurrences":[...]}
# Occurrence 0 is the anchor itself, unchanged; the rest are ordinary bookings.

# 14. Read the agreement back, and change one occurrence out of the pattern
SERIES=$(curl -s -X POST $B/series -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: series-1' \
  -d "{\"anchor_reference\":\"$REF\",\"count\":4,\"interval_weeks\":1}" \
  | sed 's/.*"series_id":"\([^"]*\)".*/\1/')
curl -s $B/series/$SERIES -H "Authorization: Bearer $TOKEN"
# ... and the second occurrence's reference, from that response:
NEXT=$(curl -s $B/series/$SERIES -H "Authorization: Bearer $TOKEN" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["occurrences"][1]["reference"])')
curl -s -X PATCH $B/reservations/$NEXT -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"party_size":2}'
curl -s $B/series/$SERIES -H "Authorization: Bearer $TOKEN"
# that occurrence is now "exception": true and the agreement is at revision 2
```

A history, decision or agreement read by anybody but its owner is `404 not_found`,
including with no token at all — none of them can be used to find out whether an
identifier exists.

## Stage 4 in the same smoke test

A table comes out of service, the room proposes a repair, the manager applies it,
and the diner moves the rest of their agreement. These continue from the fixture,
`$TOKEN`, `$REF` and `$SERIES` above — where the anchor booking sits on `t_2` at
19:00 with a party of three, and the agreement has four Thursdays, the second of
them already an exception.

```bash
# 15. What would closing t_2 for that evening do? A manager's write, idempotent,
#     and it changes nothing: only the plan is stored.
curl -s -X POST $B/restaurants/r_anker/replans -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: plan-1' -d '{
    "table_id":"t_2","from":"2026-10-15T18:00:00+02:00","to":"2026-10-15T23:00:00+02:00"}'
# 201:
# {"plan_id":"pln_...","restaurant_revision":7,
#  "closure":{"table_id":"t_2","from":"2026-10-15T18:00:00+02:00",
#                            "to":"2026-10-15T23:00:00+02:00"},
#  "assignments":[{"reference":"(your $REF)","table_ids":["t_3"],"changed":true}],
#  "moved_count":1,"unused_seats":3}
PLAN=pln_...   # the plan_id from that response
# restaurant_revision is the room's own counter: 7 after steps 4, 8, 10, 11, 12, 13
# and 14, none of which were no-ops.
# Only the confirmed booking that overlaps the interval is considered: the three
# later Thursdays are outside it and the cancelled pair booking is not a candidate
# at all. t_3 seats six and the party is three, hence unused_seats 3 — the fewest
# changes first, then the fewest empty seats, then the earliest option.
# A diner who is not a manager gets 403; seven tables or more, five declared pairs
# or seven considered bookings would be 422 planning_limit; a closure with no seating
# that works would be 409 no_feasible_plan, stored nowhere.

# 16. Apply it: the closure and every assignment, in one transaction
curl -s -X POST $B/restaurants/r_anker/replans/$PLAN/apply -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: apply-1' -d '{}'
# 201 {"plan_id":"pln_...","restaurant_revision":8,"reservations":[{
#   "reference":"(your $REF)","table_id":"t_3","table_ids":["t_3"],"party_size":3,
#   "starts_at_local":"2026-10-15T19:00","revision":3,
#   "accepted_terms":{"policy_version":0,...}}]}
# Times, party size and the accepted terms are identical; only the table moved.
# Replaying apply-1 returns this body with 200; the same plan under a different key
# is 409 plan_already_applied; any write to this restaurant in between — a booking,
# an amendment, a policy — makes it 409 stale_plan and changes nothing, because the
# arrangement was computed against a room that is no longer there.

# 17. The diner's agreement moved with it, and counted once for the whole plan
curl -s $B/series/$SERIES -H "Authorization: Bearer $TOKEN"
# revision 3: occurrence 0 now holds ["t_3"] at the same local time, its
# "exception" flag still false; occurrence 1 is still the exception the diner made,
# on t_2, on the date and at the time they put it.

# 18. The booking's own record says a table moved, and names the plan that moved it
curl -s $B/reservations/$REF/history -H "Authorization: Bearer $TOKEN"
# created, then changed (party_size 4 -> 3), then
# {"event":"reassigned","revision":3,"plan_id":"pln_...",
#  "changes":[{"field":"table_ids","from":["t_2"],"to":["t_3"]}],
#  "accepted_terms":{"policy_version":0,...}}
# A booking the plan did not move gains neither a revision nor an entry.

# 19. t_2 is out of service now, and so is every pair containing it
curl -s "$B/availability?restaurant_id=r_anker&date=2026-10-15&party_size=4&explain=true"
# At 19:00 the slot offers nothing: t_3 holds the moved booking, t_1 seats two, and
# t_2 reports {"rule":"no_overlap","holds":false} because of the closure — the same
# answer a conflicting booking would get. Neither declared pair is offered, because
# both contain t_2. At 21:00 only t_3 is.
curl -s -X POST $B/reservations -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-20' -d '{
    "restaurant_id":"r_anker","table_id":"t_2",
    "starts_at_local":"2026-10-15T21:00","party_size":2}'
# 409 {"error":{"code":"table_unavailable","message":"Table 't_2' is closed from
#      2026-10-15T18:00:00+02:00 to 2026-10-15T23:00:00+02:00"}}

# 20. Move the rest of the agreement to a new clock time, in one write
curl -s -X POST $B/series/$SERIES/amend -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: amend-1' -d '{
    "expected_revision":3,"from_index":1,"local_time":"20:00"}'
# 201, and the answer is the agreement as it now reads, at revision 4:
#   0  2026-10-15T19:00 +02:00   before from_index, so untouched
#   1  2026-10-22T19:00 +02:00   the diner's exception, left where they put it
#   2  2026-10-29T20:00 +01:00   each occurrence keeps its own local date, and
#   3  2026-11-05T20:00 +01:00   the offsets follow the calendar past 25 October
# Every moved occurrence gained one revision and one ordinary "changed" entry with
# {"field":"starts_at_local","from":"2026-10-29T19:00","to":"2026-10-29T20:00"},
# and the restaurant revision counted once for the whole request. Nothing is marked
# an exception: the pattern itself moved.
# Replaying amend-1 returns this body with 200; a revision the agreement no longer
# has is 409 stale_revision, judged before any cutoff or sitting; asking for 19:00
# again would succeed as a real change, not as a no-op.
```

## The same flow in the browser

Open `http://localhost:8080/`, sign in as
`ada@example.com` / `correct horse`, search Zum Anker for a party of seven, take
the "Tables 2 & 3" cell, confirm, then copy the reference into
`http://localhost:8080/lookup`. No new screen is required by this stage: the grid,
the booking form, the confirmation and the lookup are the same screens, and a
booking's revision and accepted terms travel in the responses they already read.
An applied plan shows up in them without any of them changing — the grid stops
offering the closed table and the pairs containing it, the confirmation and the
lookup name the tables the booking holds now, and the reference the diner was
given still finds their booking after the room moved it.

## Running the tests

The suite talks to the service over HTTP through the ASGI app, so it needs no
container and no database server:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

887 tests: 697 of them are the earlier stages' suites, run unchanged against this
service, and the rest cover closures, seating plans and agreement-wide
rescheduling (`tests/test_replans.py`, `tests/test_series_amend.py`) and the import
of a stage 1, 2 or 3 snapshot (`tests/test_export_import.py`).

The browser half is checked two ways. `tests/test_screens.py` (32 checks) and
`tests/test_bookings_screen.py` (15 checks) assert what the routes serve: HTML,
the named controls, a label on every input, and no reference to any other host.
`tools/ui-check.mjs` loads those same screens into a DOM and drives the product's
own script against a running service, which is how the behaviour that only a
browser can show — a late search response, a lost booking response and its retry,
a refusal that refreshes the grid, the diner's own list and a cancellation made
from it — is verified where no browser is installed:

```bash
cd tools && npm install jsdom && node ui-check.mjs http://localhost:8080
```

## Running without Docker

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PORT=8080 .venv/bin/python -m tablekeeper.serve
```

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `PORT` | `8080` | Port to listen on, bound to `0.0.0.0` |
| `TABLEKEEPER_DB` | `/tmp/tablekeeper/tablekeeper.db` | SQLite file location |
| `HOST` | `0.0.0.0` | Bind address |

State is ephemeral by design: it lives in the container filesystem and need not
survive a restart. `POST /_test/reset`, `GET /_test/export` and
`POST /_test/import` are enabled in the delivered image and require no
authentication. A snapshot exported by any earlier stage of this service imports
here: bookings that predate table combinations are rebuilt from the single table
their own record names, bookings that predate policies are given the seeded terms
and a `created` entry, the stage-4 tables are added empty, a browser signed in
before the import stays signed in — and the imported agreements and bookings work
with this stage's operations, so an imported agreement can still be rescheduled and
an imported booking can still be moved by a plan.
