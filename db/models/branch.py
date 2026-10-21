from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, Float, UniqueConstraint, CheckConstraint
from sqlalchemy.orm import relationship
from db.database import Base

class Branch(Base):
    __tablename__ = "branches"
    
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    name = Column(String, index=True, nullable=False)
    address = Column(String, nullable=False)
    city = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    image_url = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    
    tenant = relationship("Tenant", back_populates="branches")
    inventories = relationship("Inventory", back_populates="branch")

class Inventory(Base):
    __tablename__ = "inventories"
    __table_args__ = (UniqueConstraint('branch_id', 'variant_id', name='uq_inventory_branch_variant'),
                      CheckConstraint('stock >= 0', name='ck_inventory_stock_nonnegative'))
    
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    branch_id = Column(Integer, ForeignKey("branches.id"), nullable=False)
    variant_id = Column(Integer, ForeignKey("product_variants.id"), nullable=False)
    stock = Column(Integer, default=0, nullable=False)
    
    tenant = relationship("Tenant")
    branch = relationship("Branch", back_populates="inventories")
    variant = relationship("ProductVariant", back_populates="inventories")

