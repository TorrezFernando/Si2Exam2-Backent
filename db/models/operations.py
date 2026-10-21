from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime, Text
from sqlalchemy.sql import func
from db.database import Base


class Supplier(Base):
    __tablename__ = 'suppliers'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=True, index=True)
    name = Column(String, nullable=False)
    contact = Column(String)
    is_active = Column(Boolean, nullable=False, default=True)


class Season(Base):
    __tablename__ = 'seasons'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=True, index=True)
    name = Column(String, nullable=False)
    description = Column(Text)


class Collection(Base):
    __tablename__ = 'collections'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=True, index=True)
    name = Column(String, nullable=False)
    description = Column(Text)


class InventoryMovement(Base):
    __tablename__ = 'inventory_movements'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=True, index=True)
    branch_id = Column(Integer, ForeignKey('branches.id'), nullable=False, index=True)
    variant_id = Column(Integer, ForeignKey('product_variants.id'), nullable=False)
    quantity = Column(Integer, nullable=False)
    kind = Column(String, nullable=False)
    reference = Column(String, nullable=False)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

