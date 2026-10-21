from typing import List, Optional, Any
from datetime import datetime
from pydantic import BaseModel, Field
from db.models.sale import OrderTypeEnum, PaymentMethodEnum, PaymentStatusEnum

class OrderItemBase(BaseModel):
    variant_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=10000)
    unit_price: float = Field(ge=0, allow_inf_nan=False)

class OrderItemCreate(OrderItemBase):
    unit_price: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)

class ProductSummary(BaseModel):
    id: int
    name: str
    image_url: Optional[str] = None
    
    class Config:
        from_attributes = True

class VariantSummary(BaseModel):
    id: int
    size: str
    color: str
    product: Optional[ProductSummary] = None
    image_url: Optional[str] = None
    
    class Config:
        from_attributes = True

class OrderItem(OrderItemBase):
    id: int
    order_id: int
    variant: Optional[VariantSummary] = None

    class Config:
        from_attributes = True

class OrderBase(BaseModel):
    branch_id: Optional[int] = None
    order_type: OrderTypeEnum = OrderTypeEnum.presencial
    payment_method: PaymentMethodEnum = PaymentMethodEnum.tarjeta
    payment_status: PaymentStatusEnum = PaymentStatusEnum.paid
    total_amount: float
    is_paid: bool = True
    payment_provider: Optional[str] = None
    card_type: Optional[str] = None
    card_last_four: Optional[str] = None
    transaction_ref: Optional[str] = None

class OrderCreate(BaseModel):
    order_type: Optional[OrderTypeEnum] = None
    reservation_id: Optional[int] = Field(default=None, gt=0)
    request_key: Optional[str] = Field(default=None, min_length=8, max_length=100)
    branch_id: Optional[int] = None
    items: List[OrderItemCreate] = Field(min_length=1, max_length=100)
    # Atributos de pago simulados (se pasarán desde el frontend)
    payment_method: PaymentMethodEnum = PaymentMethodEnum.tarjeta
    card_type: Optional[str] = None
    card_last_four: Optional[str] = None
    transaction_ref: Optional[str] = None

class Order(OrderBase):
    payment_url: Optional[str] = None
    reservation_id: Optional[int] = None
    id: int
    user_id: int
    tenant_id: Optional[int] = None
    client_operation_uuid: Optional[str] = None
    is_offline_operation: bool = False
    sync_status: str = "CONFIRMADA"
    rejection_reason: Optional[str] = None
    created_at: datetime
    items: List[OrderItem]

    class Config:
        from_attributes = True


class GarmentExchangeCreate(BaseModel):
    original_variant_id: int = Field(..., gt=0, description="ID de la variante que se devuelve")
    new_variant_id: int = Field(..., gt=0, description="ID de la nueva variante deseada (talla/color)")
    quantity: int = Field(1, ge=1, description="Cantidad de prendas a cambiar")
    branch_id: int = Field(..., gt=0, description="Sucursal donde se realiza el cambio físico")
    reason: Optional[str] = Field("Cambio de talla/color", max_length=255)


class GarmentExchangeSchema(BaseModel):
    id: int
    tenant_id: Optional[int] = None
    order_id: int
    branch_id: int
    user_id: int
    original_variant_id: int
    new_variant_id: int
    quantity: int
    reason: Optional[str] = None
    difference_amount: float
    status: str
    created_at: datetime
    days_since_purchase: Optional[int] = None
    exchange_window_days: Optional[int] = None

    class Config:
        from_attributes = True

