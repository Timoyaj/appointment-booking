from uuid import UUID, uuid4
from .db import pool
from typing import Optional
from datetime import datetime

async def list_slots():
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, start_time, status, booked_by, booking_id FROM slots ORDER BY start_time")
        return [dict(row) for row in rows]

async def book_slot(slot_id: UUID, patient_name: str):
    """Atomic booking:
    - Only books if slot.status = 'open'
    - Enforces timing rule: cannot book within 1 hour of start_time
    Returns booking row on success, None on failure.
    """
    booking_id = uuid4()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Try to atomically mark the slot booked and insert a booking in the same transaction.
            # First do the conditional update returning the slot row.
            updated = await conn.fetchrow(
                """
                UPDATE slots
                SET status = 'booked', booking_id = $1, booked_by = $2
                WHERE id = $3
                  AND status = 'open'
                  AND start_time > now() + interval '1 hour'
                RETURNING id
                """,
                booking_id, patient_name, slot_id
            )
            if not updated:
                return None
            # Insert booking record
            booking_row = await conn.fetchrow(
                "INSERT INTO bookings (id, slot_id, patient_name) VALUES ($1, $2, $3) RETURNING id, slot_id, patient_name, created_at",
                booking_id, slot_id, patient_name
            )
            return dict(booking_row)

async def cancel_booking(booking_id: UUID):
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Find booking
            bk = await conn.fetchrow("SELECT id, slot_id FROM bookings WHERE id = $1", booking_id)
            if not bk:
                return False
            slot_id = bk["slot_id"]
            # Delete booking
            await conn.execute("DELETE FROM bookings WHERE id = $1", booking_id)
            # Mark slot open again (clear booking_id/booked_by)
            await conn.execute("UPDATE slots SET status = 'open', booking_id = NULL, booked_by = NULL WHERE id = $1", slot_id)
            return True
