from pydantic import BaseModel
from typing import Optional
from uuid import UUID
from datetime import datetime

class Slot(BaseModel):
    id: UUID
    start_time: datetime
    status: str
    booked_by: Optional[str]
    booking_id: Optional[UUID]

class BookingRequest(BaseModel):
    slot_id: UUID
    patient_name: str

class BookingResponse(BaseModel):
    id: UUID
    slot_id: UUID
    patient_name: str
    created_at: datetime
