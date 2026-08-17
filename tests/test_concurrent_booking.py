import asyncio
import os
import pytest
import httpx
from uuid import UUID
from app import main as app_module
from app import db as db_module
from app import crud

# These tests expect DATABASE_URL to point to a running test DB with the schema created
# Start the DB with docker-compose up -d before running tests.

import pytest

@pytest.mark.asyncio
async def test_two_simultaneous_bookings():
    # Ensure DB pool is connected for test client to use
    await db_module.connect()

    # Get a slot id (we inserted one in db/init.sql)
    slots = await crud.list_slots()
    assert slots, "No slots found; ensure db/init.sql created a slot"
    slot = slots[0]
    slot_id = slot["id"]

    async def attempt_book(name):
        async with httpx.AsyncClient(app=app_module.app, base_url="http://test") as client:
            resp = await client.post("/bookings", json={"slot_id": slot_id, "patient_name": name})
            return resp

    # Fire two booking attempts concurrently
    r1_task = asyncio.create_task(attempt_book("Alice"))
    r2_task = asyncio.create_task(attempt_book("Bob"))
    r1, r2 = await asyncio.gather(r1_task, r2_task)

    statuses = {r1.status_code, r2.status_code}
    # One should be 200, the other 409
    assert statuses == {200, 409}, f"Expected one 200 and one 409, got {r1.status_code} and {r2.status_code}"

    # Cleanup: if booking succeeded, cancel it so repeated test runs can work
    success_resp = r1 if r1.status_code == 200 else r2 if r2.status_code == 200 else None
    if success_resp:
        booking_id = success_resp.json()["id"]
        async with httpx.AsyncClient(app=app_module.app, base_url="http://test") as client:
            await client.delete(f"/bookings/{booking_id}")

    await db_module.close()
