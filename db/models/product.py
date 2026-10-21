from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, Float, Text
from sqlalchemy.orm import relationship
from db.database import Base

class Category(Base):
    __tablename__ = "categories"
    
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    name = Column(String, index=True, nullable=False)
    description = Column(Text, nullable=True)
    image_url = Column(String, nullable=True)
    
    products = relationship("Product", back_populates="category")

class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    name = Column(String, index=True, nullable=False)
    description = Column(Text)
    base_price = Column(Float, nullable=False)
    category_id = Column(Integer, ForeignKey("categories.id"))
    season = Column(String) # e.g., Summer 2026
    is_active = Column(Boolean, nullable=False, default=True, server_default='true')
    supplier_id = Column(Integer, ForeignKey('suppliers.id'), nullable=True)
    season_id = Column(Integer, ForeignKey('seasons.id'), nullable=True)
    collection_id = Column(Integer, ForeignKey('collections.id'), nullable=True)
    image_url = Column(String, nullable=True)
    
    tenant = relationship("Tenant", back_populates="products")
    category = relationship("Category", back_populates="products")
    variants = relationship("ProductVariant", back_populates="product", cascade="all, delete-orphan")

    # ─── Virtual Try-On IA ────────────────────────────────────────────────────
    garment_image_url = Column(String, nullable=True)   # Foto prenda (para IA)
    garment_type = Column(String, nullable=True)         # upper_body | lower_body | dresses

class ProductVariant(Base):
    __tablename__ = "product_variants"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)

    size = Column(String, nullable=False) # e.g., S, M, L, XL
    color = Column(String, nullable=False) # e.g., Red, Blue
    sku = Column(String, unique=True, index=True, nullable=False)
    price_override = Column(Float, nullable=True) # Optional different price for specific variant
    image_url = Column(String, nullable=True)
    
    product = relationship("Product", back_populates="variants")
    inventories = relationship("Inventory", back_populates="variant", cascade="all, delete-orphan")

    @property
    def quantity(self) -> int:
        if not self.inventories:
            return 0
        return sum(inv.stock for inv in self.inventories if inv.stock is not None)

    @quantity.setter
    def quantity(self, value):
        # quantity is now a computed field (sum of all branch inventories).
        # Do NOT distribute the total back to branches — manage inventories directly.
        pass
