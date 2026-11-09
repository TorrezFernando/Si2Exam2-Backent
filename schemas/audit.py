from typing import Optional, Any
from datetime import datetime
from pydantic import BaseModel


class AuditLogSchema(BaseModel):
    id: int
    user_id: Optional[int] = None
    user_email: Optional[str] = None
    action: str
    entity_type: str
    entity_id: Optional[str] = None
    details: Optional[Any] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    created_at: datetime
