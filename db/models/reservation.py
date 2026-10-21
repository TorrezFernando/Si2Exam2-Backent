from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime, Enum, Float, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import enum
from db.database import Base

class ReservationStatus(str, enum.Enum):
    pending = "pending"
    confirmed = "confirmed"
    cancelled = "cancelled"
    completed = "completed"

class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (
        UniqueConstraint('tenant_id', 'client_operation_uuid', name='uq_reservation_tenant_client_uuid'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    branch_id = Column(Integer, ForeignKey("branches.id"), nullable=False) # Tienda seleccionada obligatoria
    status = Column(Enum(ReservationStatus), default=ReservationStatus.pending)
    pickup_date = Column(DateTime(timezone=True), nullable=True) # Fecha estimada/programada de retiro
    notes = Column(String, nullable=True) # Comentarios o notas de la reserva
    
    # Sincronización e Idempotencia Offline (RNF04, RNF05)
    client_operation_uuid = Column(String, index=True, nullable=True)
    is_offline_operation = Column(Boolean, default=False, nullable=False)
    sync_status = Column(String, default="CONFIRMADA", nullable=False)
    rejection_reason = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    tenant = relationship("Tenant")
    user = relationship("User")
    branch = relationship("Branch")
    items = relationship("ReservationItem", back_populates="reservation", cascade="all, delete-orphan")

class ReservationItem(Base):
    __tablename__ = "reservation_items"
    
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    reservation_id = Column(Integer, ForeignKey("reservations.id"), nullable=False)
    variant_id = Column(Integer, ForeignKey("product_variants.id"), nullable=False)
    quantity = Column(Integer, default=1, nullable=False)
    unit_price = Column(Float, nullable=True)
    
    tenant = relationship("Tenant")
    reservation = relationship("Reservation", back_populates="items")
    variant = relationship("ProductVariant")

