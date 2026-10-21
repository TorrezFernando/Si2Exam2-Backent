from typing import Optional
from pydantic import BaseModel, ConfigDict

class BranchBase(BaseModel):
    city: Optional[str] = None
    name: str
    address: str
    phone: Optional[str] = None
    is_active: bool = True
    tenant_id: Optional[int] = None


class BranchCreate(BranchBase):
    pass

class BranchUpdate(BaseModel):
    city: Optional[str] = None
    name: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    is_active: Optional[bool] = None

class Branch(BranchBase):
    id: int
    model_config = ConfigDict(from_attributes=True)
