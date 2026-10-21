from sqlalchemy import Column, Integer, String, JSON, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from db.database import Base

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True) # Who did it
    action = Column(String, nullable=False, index=True) # CREATE, UPDATE, DELETE
    entity_type = Column(String, nullable=False, index=True) # USER, ROLE, PRODUCT, etc.
    entity_id = Column(String, nullable=True) # ID of the modified entity
    details = Column(JSON, nullable=True) # Old values vs New values
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    tenant = relationship("Tenant")
    user = relationship("User")

