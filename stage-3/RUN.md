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

The API and the browser product are one process on one port. The four screens are
reachable by URL and return HTML:

| Route | Screen |
| --- | --- |
| `http://localhost:8080/` | Search and the availability grid |
| `http://localhost:8080/signup` | Create an account |
| `http://localhost:8080/login` | Sign in |
| `http://localhost:8080/lookup` | Look up a booking by reference |

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

## The same flow in the browser

Open `http://localhost:8080/`, sign in as
`ada@example.com` / `correct horse`, search Zum Anker for a party of seven, take
the "Tables 2 & 3" cell, confirm, then copy the reference into
`http://localhost:8080/lookup`. No new screen is required by this stage: the grid,
the booking form, the confirmation and the lookup are the same screens, and a
booking's revision and accepted terms travel in the responses they already read.

## Running the tests

The suite talks to the service over HTTP through the ASGI app, so it needs no
container and no database server:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The browser half is checked two ways. `tests/test_screens.py` (32 checks) asserts what the
routes serve: HTML, the named controls, a label on every input, and no reference
to any other host. `tools/ui-check.mjs` loads those same screens into a DOM and
drives the product's own script against a running service, which is how the
behaviour that only a browser can show — a late search response, a lost booking
response and its retry, a refusal that refreshes the grid — is verified where no
browser is installed:

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
authentication. A snapshot exported by the preceding stage imports here: its
bookings predate table combinations, so each is rebuilt from the single table its
own record names, and a browser signed in before the import stays signed in.
