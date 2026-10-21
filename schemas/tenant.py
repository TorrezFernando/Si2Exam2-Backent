from pydantic import BaseModel, ConfigDict
from typing import Optional, List
from datetime import datetime


class SubscriptionPlanBase(BaseModel):
    name: str
    code: str
    max_branches: int = 2
    has_ai_reports: bool = False
    has_offline_sync: bool = True
    price: float = 0.0
    is_active: bool = True


class SubscriptionPlanCreate(SubscriptionPlanBase):
    pass


class SubscriptionPlan(SubscriptionPlanBase):
    id: int
    created_at: Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)


class SubscriptionBase(BaseModel):
    tenant_id: int
    plan_id: int
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    status: str = "active"


class SubscriptionCreate(SubscriptionBase):
    pass


class Subscription(SubscriptionBase):
    id: int
    created_at: Optional[datetime] = None
    plan: Optional[SubscriptionPlan] = None
    model_config = ConfigDict(from_attributes=True)


class TenantBase(BaseModel):
    name: str
    subdomain: str
    is_active: bool = True
    exchange_window_days: int = 30


class TenantCreate(TenantBase):
    plan_code: Optional[str] = "basic"


class Tenant(TenantBase):
    id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    subscriptions: List[Subscription] = []
    model_config = ConfigDict(from_attributes=True)
