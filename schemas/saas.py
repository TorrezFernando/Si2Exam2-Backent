from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


MODEL_PATTERN = r'^gemini-[a-z0-9][a-z0-9.\-]{1,50}$'


class SubscriptionPlanSchema(BaseModel):
    id: int
    name: str
    code: str
    description: Optional[str] = None
    max_branches: int
    max_users: Optional[int] = None
    max_products: Optional[int] = None
    has_ai_reports: bool
    has_ai_shopper: bool = True
    has_ai_tryon: bool = True
    ai_text_model: Optional[str] = None
    ai_image_model: Optional[str] = None
    has_offline_sync: bool
    price: float
    is_active: bool
    subscribers_count: int = 0
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class PlanUpdate(BaseModel):
    """Campos editables por el Superadmin. Un límite en null significa ilimitado."""
    name: Optional[str] = Field(None, min_length=2, max_length=80)
    description: Optional[str] = Field(None, max_length=255)
    price: Optional[float] = Field(None, ge=0, le=100000, allow_inf_nan=False)
    max_branches: Optional[int] = Field(None, ge=1, le=1000)
    max_users: Optional[int] = Field(None, ge=1, le=100000)
    max_products: Optional[int] = Field(None, ge=1, le=1000000)
    has_ai_reports: Optional[bool] = None
    has_ai_shopper: Optional[bool] = None
    has_ai_tryon: Optional[bool] = None
    has_offline_sync: Optional[bool] = None
    ai_text_model: Optional[str] = Field(None, pattern=MODEL_PATTERN)
    ai_image_model: Optional[str] = Field(None, pattern=MODEL_PATTERN)
    is_active: Optional[bool] = None


class PlanCreate(PlanUpdate):
    code: str = Field(..., min_length=2, max_length=30, pattern=r'^[a-z0-9][a-z0-9_\-]*$')
    name: str = Field(..., min_length=2, max_length=80)
    price: float = Field(..., ge=0, le=100000, allow_inf_nan=False)
    max_branches: int = Field(..., ge=1, le=1000)


class AIModelOptions(BaseModel):
    text_models: List[str]
    image_models: List[str]
    default_text_model: str
    default_image_model: str


class TenantBase(BaseModel):
    name: str = Field(..., min_length=2, max_length=150)
    subdomain: str = Field(..., min_length=2, max_length=50)
    exchange_window_days: int = Field(2, ge=1, le=2)
    is_active: bool = True


class TenantCreate(TenantBase):
    plan_code: str = "basic"
    admin_email: Optional[EmailStr] = None
    admin_password: Optional[str] = None
    admin_name: Optional[str] = None


class TenantUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=150)
    subdomain: Optional[str] = Field(None, min_length=2, max_length=50)
    exchange_window_days: Optional[int] = Field(None, ge=1, le=2)
    is_active: Optional[bool] = None


class TenantSchema(TenantBase):
    id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class TenantDetailSchema(TenantSchema):
    active_plan: Optional[SubscriptionPlanSchema] = None
    subscription_status: str = "active"
    subscription_end_date: Optional[datetime] = None
    cancel_at_period_end: bool = True
    days_remaining: Optional[int] = None
    branches_count: int = 0
    users_count: int = 0
    orders_count: int = 0


class ChangePlanRequest(BaseModel):
    plan_code: str = Field(..., description="Código del nuevo plan ('basic', 'professional', 'enterprise')")


class SubscriptionCheckoutRequest(BaseModel):
    kind: str = Field(..., pattern="^(change_plan|renew)$")
    plan_code: Optional[str] = Field(None, max_length=30)


class SubscriptionCheckoutResponse(BaseModel):
    payment_id: int
    kind: str
    status: str            # pending | applied | failed | cancelled
    amount: float
    currency: str
    provider: str
    payment_url: Optional[str] = None
    plan: Optional[SubscriptionPlanSchema] = None
    error: Optional[str] = None
    created_at: Optional[datetime] = None
    paid_at: Optional[datetime] = None
    tenant: Optional["TenantDetailSchema"] = None


class AutoCancelUpdateRequest(BaseModel):
    cancel_at_period_end: bool = Field(..., description="Si es True, la suscripción se cancela automáticamente al acabar el mes")


class SubscriptionUsage(BaseModel):
    branches_count: int
    branches_limit: int
    users_count: int
    users_limit: Optional[int] = None
    staff_count: int = 0
    products_count: int
    products_limit: Optional[int] = None
    orders_count: int


class MySubscriptionResponse(BaseModel):
    is_superadmin: bool
    tenant: Optional[TenantSchema] = None
    plan: Optional[SubscriptionPlanSchema] = None
    status: str = "active"
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    cancel_at_period_end: bool = True
    cancelled_at: Optional[datetime] = None
    days_remaining: Optional[int] = None
    usage: Optional[SubscriptionUsage] = None


class SaaSStatsResponse(BaseModel):
    total_tenants: int
    active_tenants: int
    total_branches: int
    total_users: int
    total_orders: int
    plans_distribution: Dict[str, int]
    monthly_recurring_revenue: float
