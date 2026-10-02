# RUN.md

## One command: build and start

```bash
docker build -t tablekeeper . && docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper
```

That is the whole setup. The image is self-contained: Python, FastAPI/uvicorn,
the IANA timezone database and the application code are installed at build time,
and nothing is fetched when the container runs. The service listens on
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

A complete smoke test — load a fixture, sign in, search, book:

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
      "tables": [{"id":"t_1","label":"1","capacity":2},{"id":"t_2","label":"2","capacity":4}]
    }],
    "reservations": []}'
# 204

# 2. Log in as the seeded user
TOKEN=$(curl -s -X POST $B/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"ada@example.com","password":"correct horse"}' | sed 's/.*"token":"\([^"]*\)".*/\1/')

# 3. Search (public, no token)
curl -s "$B/availability?restaurant_id=r_anker&date=2026-09-24&party_size=4"

# 4. Book, idempotently
curl -s -X POST $B/reservations -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-1' -d '{
    "restaurant_id":"r_anker","table_id":"t_2",
    "starts_at_local":"2026-09-24T19:00","party_size":4}'
# 201, with a reference such as "K3P7QW"

# 5. Retry the identical request: 200 with the same body, no second booking
curl -s -o /dev/null -w '%{http_code}\n' -X POST $B/reservations \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-1' -d '{
    "restaurant_id":"r_anker","table_id":"t_2",
    "starts_at_local":"2026-09-24T19:00","party_size":4}'
# 200
```

## Running the tests

The suite talks to the service over HTTP through the ASGI app, so it needs no
container and no database server:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
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
authentication.
