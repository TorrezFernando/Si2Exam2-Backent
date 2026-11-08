from typing import Any, List, Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from api.deps import get_db, require_permission
from core.access import is_global_admin
from db.models.audit import AuditLog
from db.models.user import User
from schemas.audit import AuditLogSchema

router = APIRouter()

@router.get("", response_model=List[AuditLogSchema])
def get_audit_logs(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("audit:read")),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    user_id: Optional[int] = None,
    action: Optional[str] = None,
    entity_type: Optional[str] = None,
) -> Any:
    """
    Obtiene la bitácora de auditoría separando eventos SaaS globales vs eventos de empresa.
    """
    query = db.query(AuditLog)
    if is_global_admin(current_user):
        query = query.filter(
            or_(
                AuditLog.tenant_id.is_(None),
                AuditLog.entity_type.in_(["TENANT", "SUBSCRIPTION", "ROLE", "AUTH"]),
            )
        )
    else:
        query = query.filter(AuditLog.tenant_id == current_user.tenant_id)
    
    if user_id is not None:
        query = query.filter(AuditLog.user_id == user_id)
    if action:
        query = query.filter(AuditLog.action == action)
    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type)
        
    return query.order_by(AuditLog.created_at.desc()).offset(skip).limit(limit).all()
