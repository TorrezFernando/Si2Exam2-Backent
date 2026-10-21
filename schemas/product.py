from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field

# --- Category ---
class CategoryBase(BaseModel):
    name: str
    description: Optional[str] = None
    image_url: Optional[str] = None

class CategoryCreate(CategoryBase):
    pass

class Category(CategoryBase):
    id: int
    model_config = ConfigDict(from_attributes=True)

class InventorySchema(BaseModel):
    branch_id: int
    stock: int = Field(default=0, ge=0)
    model_config = ConfigDict(from_attributes=True)

class InventoryInput(BaseModel):
    branch_id: int
    stock: int = Field(default=0, ge=0)

# --- Product Variant ---
class ProductVariantBase(BaseModel):
    size: str
    color: str
    sku: Optional[str] = None
    quantity: int = Field(default=0, ge=0)
    branch_id: Optional[int] = None
    price_override: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    image_url: Optional[str] = None
    inventories: Optional[List[InventorySchema]] = None

class ProductVariantCreate(ProductVariantBase):
    inventories: Optional[List[InventoryInput]] = None

class ProductVariantUpdate(ProductVariantBase):
    id: Optional[int] = None
    inventories: Optional[List[InventoryInput]] = None

class ProductVariant(ProductVariantBase):
    id: int
    product_id: int
    inventories: List[InventorySchema] = []
    model_config = ConfigDict(from_attributes=True)

# --- Product ---
class ProductBase(BaseModel):
    supplier_id: Optional[int] = None
    season_id: Optional[int] = None
    collection_id: Optional[int] = None
    name: str
    description: Optional[str] = None
    base_price: float = Field(ge=0, allow_inf_nan=False)
    category_id: Optional[int] = None
    season: Optional[str] = None
    branch_id: Optional[int] = None
    image_url: Optional[str] = None
    garment_image_url: Optional[str] = None
    garment_type: Optional[str] = None

class ProductCreate(ProductBase):
    variants: List[ProductVariantCreate] = []

class ProductUpdate(BaseModel):
    supplier_id: Optional[int] = None
    season_id: Optional[int] = None
    collection_id: Optional[int] = None
    name: Optional[str] = None
    description: Optional[str] = None
    base_price: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    category_id: Optional[int] = None
    season: Optional[str] = None
    branch_id: Optional[int] = None
    image_url: Optional[str] = None
    garment_image_url: Optional[str] = None
    garment_type: Optional[str] = None
    variants: Optional[List[ProductVariantUpdate]] = None

class Product(ProductBase):
    is_active: bool = True
    id: int
    category: Optional[Category] = None
    variants: List[ProductVariant] = []
    has_tryon: bool = False
    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def model_validate(cls, obj, *args, **kwargs):
        instance = super().model_validate(obj, *args, **kwargs)
        # has_tryon = True si tiene imagen de prenda para IA
        if hasattr(obj, 'garment_image_url') or hasattr(obj, 'image_url'):
            instance.has_tryon = bool(
                getattr(obj, 'garment_image_url', None) or getattr(obj, 'image_url', None)
            )
        return instance
