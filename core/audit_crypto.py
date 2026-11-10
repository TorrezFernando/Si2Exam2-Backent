"""Bitácora confidencial: cada registro se guarda cifrado (Fernet / AES-128-CBC + HMAC-SHA256).

La llave del desarrollador (AUDIT_LOG_KEY) vive en el entorno del servidor, nunca en la base
de datos: quien abra PostgreSQL solo ve texto cifrado. El contenido únicamente se puede leer
desde el sistema (pantalla Bitácora), que descifra en el backend tras validar permisos.
"""
import base64
import contextvars
import hashlib
import json
from datetime import datetime, timezone
from cryptography.fernet import Fernet, InvalidToken
from core.config import settings

# Contexto de la solicitud HTTP en curso (lo completa AuditContextMiddleware).
request_ip = contextvars.ContextVar('audit_request_ip', default=None)
request_agent = contextvars.ContextVar('audit_request_agent', default=None)
request_user = contextvars.ContextVar('audit_request_user', default=None)

SEALED = 'CIFRADO'


def _key() -> bytes:
    secret = settings.AUDIT_LOG_KEY or settings.SECRET_KEY
    try:
        Fernet(secret.encode())  # ya es una llave Fernet válida
        return secret.encode()
    except (ValueError, TypeError):
        # Contraseña del desarrollador (p. ej. 'bitacora'): se convierte en una llave de 32 bytes.
        return base64.urlsafe_b64encode(hashlib.sha256(f'audit-log:{secret}'.encode()).digest())


def _fernet() -> Fernet:
    return Fernet(_key())


def encrypt(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data, default=str, ensure_ascii=False).encode()).decode()


def decrypt(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except (InvalidToken, ValueError):
        return None


def seal(row) -> None:
    """Cifra los datos sensibles del registro y deja visibles solo empresa y fecha (para filtrar y aislar)."""
    if row.payload:
        return
    data = {
        'user_id': row.user_id if row.user_id is not None else request_user.get(),
        'action': row.action, 'entity_type': row.entity_type, 'entity_id': row.entity_id,
        'details': row.details, 'ip': request_ip.get(), 'user_agent': request_agent.get(),
        'at': datetime.now(timezone.utc).isoformat(),
    }
    row.payload = encrypt(data)
    row.user_id = None
    row.action = SEALED
    row.entity_type = SEALED
    row.entity_id = None
    row.details = None


def open_row(row) -> dict:
    """Vista descifrada de un registro (los registros antiguos sin cifrar se muestran tal cual)."""
    data = decrypt(row.payload) if row.payload else None
    if row.payload and data is None:
        return dict(id=row.id, tenant_id=row.tenant_id, created_at=row.created_at, user_id=None,
                    action='ILEGIBLE', entity_type='LLAVE_INCORRECTA', entity_id=None,
                    details={'error': 'El registro no se puede descifrar con la llave configurada.'},
                    ip=None, user_agent=None)
    data = data or dict(user_id=row.user_id, action=row.action, entity_type=row.entity_type,
                        entity_id=row.entity_id, details=row.details, ip=None, user_agent=None)
    return dict(id=row.id, tenant_id=row.tenant_id, created_at=row.created_at,
                user_id=data.get('user_id'), action=data.get('action'), entity_type=data.get('entity_type'),
                entity_id=data.get('entity_id'), details=data.get('details'),
                ip=data.get('ip'), user_agent=data.get('user_agent'))
