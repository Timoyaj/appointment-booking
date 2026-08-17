import uvicorn
import asyncio
from fastapi import FastAPI, HTTPException
from . import db, crud, schemas
from typing import List
from uuid import UUID
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Appointment Booking Service")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

@app.on_event("startup")
async def startup():
    await db.connect()

@app.on_event("shutdown")
async def shutdown():
    await db.close()

@app.get("/slots", response_model=List[schemas.Slot])
async def get_slots():
    return await crud.list_slots()

@app.post("/bookings", response_model=schemas.BookingResponse)
async def create_booking(req: schemas.BookingRequest):
    result = await crud.book_slot(req.slot_id, req.patient_name)
    if not result:
        # Could be already booked or within 1 hour or not found
        raise HTTPException(status_code=409, detail="Slot unavailable (already booked or too close to start)")
    return result

@app.delete("/bookings/{booking_id}")
async def delete_booking(booking_id: UUID):
    ok = await crud.cancel_booking(booking_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Booking not found")
    return {"status": "cancelled"}
