import json
from sqlalchemy.orm import Session
from db.models.audit import AuditLog

def log_audit(
    db: Session,
    user_id: int | None,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    details: dict | None = None,
    tenant_id: int | None = None
):
    """
    Registra una acción en la bitácora de auditoría.
    """
    audit = AuditLog(
        tenant_id=tenant_id,
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details
    )
    db.add(audit)
    db.commit()
    db.refresh(audit)

    return audit
