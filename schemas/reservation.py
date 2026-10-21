from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel, Field, field_validator
from datetime import timezone
from db.models.reservation import ReservationStatus
from schemas.sale import VariantSummary


# ─── Item de Reserva ──────────────────────────────────────────────────────────

class ReservationItemBase(BaseModel):
    variant_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=10000)
    unit_price: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)

class ReservationItemCreate(ReservationItemBase):
    pass

class ReservationItem(ReservationItemBase):
    variant: Optional[VariantSummary] = None
    id: int
    reservation_id: int

    class Config:
        from_attributes = True


# ─── Reserva ──────────────────────────────────────────────────────────────────

class PickupDate(BaseModel):
    @field_validator("pickup_date", check_fields=False)
    @classmethod
    def future_pickup(cls, value):
        if value is not None:
            if value.tzinfo is None:
                raise ValueError("Indica la zona horaria de retiro")
            if value <= datetime.now(timezone.utc):
                raise ValueError("El horario de retiro debe ser futuro")
        return value

class ReservationCreate(PickupDate):
    branch_id: int
    pickup_date: Optional[datetime] = None
    notes: Optional[str] = None
    items: List[ReservationItemCreate] = Field(min_length=1, max_length=100)

class ReservationUpdate(PickupDate):
    status: Optional[ReservationStatus] = None
    pickup_date: Optional[datetime] = None
    notes: Optional[str] = None

class Reservation(BaseModel):
    id: int
    user_id: int
    branch_id: int
    status: ReservationStatus
    pickup_date: Optional[datetime] = None
    notes: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    items: List[ReservationItem] = []

    class Config:
        from_attributes = True
