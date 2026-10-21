from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel, Field
from db.models.sale import PaymentMethodEnum


# ─── 1. Catálogo para sincronización Offline ─────────────────────────────────

class SyncVariantItem(BaseModel):
    id: int
    sku: str
    size: str
    color: str
    price: float
    stock: int
    image_url: Optional[str] = None


class SyncProductItem(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    category_id: Optional[int] = None
    category_name: Optional[str] = None
    base_price: float
    image_url: Optional[str] = None
    variants: List[SyncVariantItem] = []


class SyncCustomerItem(BaseModel):
    id: int
    full_name: Optional[str] = None
    email: str
    phone: Optional[str] = None


class SyncCategoryItem(BaseModel):
    id: int
    name: str
    description: Optional[str] = None


class SyncCatalogResponse(BaseModel):
    tenant_id: int
    branch_id: int
    branch_name: str
    server_timestamp: datetime
    exchange_window_days: int
    categories: List[SyncCategoryItem] = []
    products: List[SyncProductItem] = []
    customers: List[SyncCustomerItem] = []


# ─── 2. Operaciones Offline (Ventas de Contado y Reservas en Lote) ─────────────

class SyncSaleItemIn(BaseModel):
    variant_id: int = Field(..., gt=0)
    quantity: int = Field(..., ge=1)
    unit_price: float = Field(..., ge=0)


class SyncSaleOperationIn(BaseModel):
    client_operation_uuid: str = Field(..., min_length=10, max_length=64, description="UUID v4 generado localmente")
    user_id: Optional[int] = Field(None, description="ID del cliente (si fue identificado)")
    created_at: Optional[datetime] = None
    payment_method: PaymentMethodEnum = PaymentMethodEnum.efectivo
    total_amount: float = Field(..., ge=0)
    items: List[SyncSaleItemIn] = Field(..., min_length=1)


class SyncReservationItemIn(BaseModel):
    variant_id: int = Field(..., gt=0)
    quantity: int = Field(1, ge=1)


class SyncReservationOperationIn(BaseModel):
    client_operation_uuid: str = Field(..., min_length=10, max_length=64, description="UUID v4 generado localmente")
    user_id: int = Field(..., gt=0, description="ID del cliente que reserva")
    pickup_date: Optional[datetime] = None
    notes: Optional[str] = None
    items: List[SyncReservationItemIn] = Field(..., min_length=1)


class SyncBatchRequest(BaseModel):
    branch_id: int = Field(..., gt=0, description="Sucursal donde se operó offline")
    sales: List[SyncSaleOperationIn] = []
    reservations: List[SyncReservationOperationIn] = []


class SyncOperationReceipt(BaseModel):
    operation_type: str  # 'sale' | 'reservation'
    client_operation_uuid: str
    status: str          # 'CONFIRMADA' | 'RECHAZADA_CONFLICTO'
    server_id: Optional[int] = None
    rejection_reason: Optional[str] = None
    processed_at: datetime


class SyncBatchResponse(BaseModel):
    total_processed: int
    confirmed_count: int
    conflict_count: int
    receipts: List[SyncOperationReceipt]
