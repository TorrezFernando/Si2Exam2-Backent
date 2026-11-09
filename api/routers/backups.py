"""Backup / Restore de todo el sistema. Solo el Superadministrador de la plataforma."""
from typing import Any
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from api.audit_logger import log_audit
from api.deps import get_db, require_superadmin
from core import backup
from db.database import engine
from db.models.user import User

router = APIRouter()
CONFIRMATION = 'RESTAURAR'
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GB


class BackupCreate(BaseModel):
    include_uploads: bool = True


class RestoreConfirm(BaseModel):
    confirmation: str


def _audit(user, action, details):
    from db.database import SessionLocal
    with SessionLocal() as db:
        log_audit(db, user.id, action, 'BACKUP', details.get('name'), details, tenant_id=None)


@router.get('')
def list_backups(_: User = Depends(require_superadmin)) -> Any:
    return backup.list_backups()


@router.post('')
def create_backup(data: BackupCreate, user: User = Depends(require_superadmin)) -> Any:
    info = backup.create_backup(engine, include_uploads=data.include_uploads)
    _audit(user, 'CREATE', info)
    return info


@router.get('/{name}/download')
def download_backup(name: str, user: User = Depends(require_superadmin)) -> Any:
    path = backup.safe_name(name)
    _audit(user, 'DOWNLOAD', {'name': name})
    return FileResponse(path, media_type='application/gzip', filename=name)


@router.delete('/{name}')
def delete_backup(name: str, user: User = Depends(require_superadmin)) -> Any:
    path = backup.safe_name(name)
    path.unlink()
    _audit(user, 'DELETE', {'name': name})
    return {'deleted': name}


@router.post('/{name}/restore')
def restore_backup(name: str, data: RestoreConfirm, user: User = Depends(require_superadmin)) -> Any:
    if data.confirmation != CONFIRMATION:
        raise HTTPException(400, f'Escribe {CONFIRMATION} para confirmar: se reemplazarán todos los datos actuales.')
    path = backup.safe_name(name)
    result = backup.restore_backup(engine, path)
    _audit(user, 'RESTORE', {'name': name, **{k: v for k, v in result.items() if k != 'tables'}})
    return result


@router.post('/upload-restore')
async def upload_and_restore(confirmation: str = Form(...), file: UploadFile = File(...),
                             user: User = Depends(require_superadmin)) -> Any:
    if confirmation != CONFIRMATION:
        raise HTTPException(400, f'Escribe {CONFIRMATION} para confirmar: se reemplazarán todos los datos actuales.')
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, 'La copia supera 1 GB.')
    from datetime import datetime, timezone
    name = f"fashionstore-subida-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.fsbak"
    path = backup.backup_dir() / name
    path.write_bytes(content)
    path.chmod(0o600)
    try:
        result = backup.restore_backup(engine, path)
    except HTTPException:
        path.unlink(missing_ok=True)
        raise
    _audit(user, 'RESTORE', {'name': name, 'uploaded_as': file.filename,
                             **{k: v for k, v in result.items() if k != 'tables'}})
    return result
