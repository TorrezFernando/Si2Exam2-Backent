from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


class SubscriptionPlanSchema(BaseModel):
    id: int
    name: str
    code: str
    max_branches: int
    has_ai_reports: bool
    has_offline_sync: bool
    price: float
    is_active: bool
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class TenantBase(BaseModel):
    name: str = Field(..., min_length=2, max_length=150)
    subdomain: str = Field(..., min_length=2, max_length=50)
    exchange_window_days: int = Field(30, ge=1, le=365)
    is_active: bool = True


class TenantCreate(TenantBase):
    plan_code: str = "basic"
    admin_email: Optional[EmailStr] = None
    admin_password: Optional[str] = None
    admin_name: Optional[str] = None


class TenantUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=150)
    subdomain: Optional[str] = Field(None, min_length=2, max_length=50)
    exchange_window_days: Optional[int] = Field(None, ge=1, le=365)
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
    branches_count: int = 0
    users_count: int = 0
    orders_count: int = 0


class ChangePlanRequest(BaseModel):
    plan_code: str = Field(..., description="Código del nuevo plan ('basic', 'professional')")


class SubscriptionUsage(BaseModel):
    branches_count: int
    branches_limit: int
    users_count: int
    products_count: int
    orders_count: int


class MySubscriptionResponse(BaseModel):
    is_superadmin: bool
    tenant: Optional[TenantSchema] = None
    plan: Optional[SubscriptionPlanSchema] = None
    status: str = "active"
    start_date: Optional[datetime] = None
    usage: Optional[SubscriptionUsage] = None


class SaaSStatsResponse(BaseModel):
    total_tenants: int
    active_tenants: int
    total_branches: int
    total_users: int
    total_orders: int
    plans_distribution: Dict[str, int]
    monthly_recurring_revenue: float
