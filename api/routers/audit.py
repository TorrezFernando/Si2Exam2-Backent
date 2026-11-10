from typing import Any, List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from api.deps import get_db, require_permission
from core.access import is_global_admin
from core.audit_crypto import open_row, verify_password
from db.models.audit import AuditLog
from db.models.user import User
from schemas.audit import AuditLogSchema

router = APIRouter()

GLOBAL_ENTITIES = {"TENANT", "SUBSCRIPTION", "SUBSCRIPTION_PLAN", "SUBSCRIPTION_PAYMENT", "ROLE", "AUTH", "BACKUP"}
SCAN_LIMIT = 5000  # registros recientes que se descifran por consulta


@router.get("", response_model=List[AuditLogSchema])
def get_audit_logs(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("audit:read")),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    user_id: Optional[int] = None,
    action: Optional[str] = None,
    entity_type: Optional[str] = None,
    ip: Optional[str] = None,
    x_audit_key: Optional[str] = Header(None),
) -> Any:
    """
    Bitácora confidencial: los registros están cifrados en la base de datos y solo se descifran aquí,
    con la llave del desarrollador del servidor, para usuarios con permiso audit:read y dentro de su ámbito.
    """
    if not verify_password(x_audit_key):
        raise HTTPException(403, "LLAVE_BITACORA: ingresa la contraseña del desarrollador para ver la bitácora.")
    query = db.query(AuditLog)
    global_admin = is_global_admin(current_user)
    if not global_admin:
        query = query.filter(AuditLog.tenant_id == current_user.tenant_id)
    rows = [open_row(r) for r in query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(SCAN_LIMIT)]
    if global_admin:
        rows = [r for r in rows if r["tenant_id"] is None or r["entity_type"] in GLOBAL_ENTITIES]
    if user_id is not None:
        rows = [r for r in rows if r["user_id"] == user_id]
    if action:
        rows = [r for r in rows if r["action"] == action]
    if entity_type:
        rows = [r for r in rows if r["entity_type"] == entity_type]
    if ip:
        rows = [r for r in rows if (r["ip"] or "").startswith(ip)]
    page = rows[skip:skip + limit]
    ids = {r["user_id"] for r in page if r["user_id"]}
    emails = dict(db.query(User.id, User.email).filter(User.id.in_(ids)).all()) if ids else {}
    return [AuditLogSchema(**{k: v for k, v in r.items() if k != "tenant_id"}, user_email=emails.get(r["user_id"])) for r in page]
