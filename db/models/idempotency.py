from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.sql import func
from db.database import Base


class IdempotencyRecord(Base):
    """Respuesta guardada de una operación enviada con Idempotency-Key (cola offline del cliente)."""
    __tablename__ = "idempotency_records"

    id = Column(Integer, primary_key=True)
    key = Column(String(80), unique=True, nullable=False, index=True)
    auth_hash = Column(String(64), nullable=False)
    method = Column(String(10), nullable=False)
    path = Column(String(255), nullable=False)
    status_code = Column(Integer, nullable=False)
    content_type = Column(String(100), nullable=True)
    body = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
