from typing import Any
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session
from api.audit_logger import log_audit
from api.deps import get_db, get_current_active_user
from core import report_builder as rb
from db.models.user import User

router = APIRouter()


@router.get('/metadata')
def metadata(db: Session = Depends(get_db), user: User = Depends(get_current_active_user)) -> Any:
    """Fuentes, columnas y criterios que el usuario puede usar según sus permisos."""
    return rb.metadata(db, user)


@router.post('/run')
def run(spec: rb.ReportRequest, db: Session = Depends(get_db), user: User = Depends(get_current_active_user)) -> Any:
    return rb.run(db, user, spec)


@router.post('/export/{fmt}')
def export(fmt: str, spec: rb.ReportRequest, db: Session = Depends(get_db),
           user: User = Depends(get_current_active_user)) -> Any:
    report = rb.run(db, user, spec)
    content, media, ext = rb.export(report, fmt)
    log_audit(db, user.id, 'EXPORT', 'REPORT', spec.source, {'format': fmt, 'rows': report['count']}, tenant_id=user.tenant_id)
    return Response(content, media_type=media,
                    headers={'Content-Disposition': f'attachment; filename="reporte-{spec.source}.{ext}"'})


@router.post('/email')
def email(spec: rb.EmailRequest, db: Session = Depends(get_db), user: User = Depends(get_current_active_user)) -> Any:
    report = rb.run(db, user, rb.ReportRequest(**spec.model_dump(exclude={'to', 'format', 'message'})))
    rb.send_email(report, spec.format, spec.to, spec.message, user.full_name or user.email)
    log_audit(db, user.id, 'EMAIL', 'REPORT', spec.source,
              {'format': spec.format, 'rows': report['count'], 'recipients': len(spec.to)}, tenant_id=user.tenant_id)
    return {'sent': True, 'recipients': spec.to, 'rows': report['count']}
