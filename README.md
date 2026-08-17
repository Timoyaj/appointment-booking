# Appointment Booking Service

FastAPI + PostgreSQL service demonstrating atomic conditional booking of slots so concurrent attempts race correctly: exactly one wins, the other gets an immediate 409 conflict.

Quick start (local)
1. Start PostgreSQL:
   docker-compose up -d

2. Set DATABASE_URL:
   export DATABASE_URL=postgresql://postgres:example@localhost:5432/appointments

3. Install dependencies and run (recommended in a venv):
   pip install -r requirements.txt
   uvicorn app.main:app --reload

4. Create slots are initialized by docker (see db/init.sql). Check GET /slots and use POST /bookings to book.

Run tests
1. Ensure the DB is running (docker-compose up -d).
2. Ensure DATABASE_URL points to the DB above.
3. pytest -q

API
- GET /slots
- POST /bookings  { "slot_id": "<uuid>", "patient_name": "Alice" }
  - returns 200 and booking info when successful, 409 when slot already taken or too close to start.
- DELETE /bookings/{id}
  - cancels booking (makes slot bookable again)
