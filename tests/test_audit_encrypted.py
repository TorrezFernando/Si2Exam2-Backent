"""Bitácora confidencial: cifrada en la base de datos, legible solo desde el sistema y con IP."""
from fastapi.testclient import TestClient
from test_sync_integrity import shop
from test_security_regressions import client
from core import audit_crypto
from core.audit_middleware import client_ip
from api.audit_logger import log_audit
from api.routers import audit
from db.models import AuditLog, Permission


def test_log_is_unreadable_in_database_but_readable_through_the_system(shop):
    token = audit_crypto.request_ip.set('181.115.10.20')
    try:
        log_audit(shop.db, shop.user.id, 'DELETE', 'PRODUCT', '7', {'nombre': 'Camisa'}, tenant_id=shop.tenant.id)
    finally:
        audit_crypto.request_ip.reset(token)
    raw = shop.db.query(AuditLog).order_by(AuditLog.id.desc()).first()
    # Lo que ve un administrador de base de datos:
    assert raw.action == raw.entity_type == 'CIFRADO' and raw.user_id is None and raw.details is None
    assert 'Camisa' not in raw.payload and '181.115' not in raw.payload
    # Lo que ve el usuario autorizado desde el sistema:
    shop.user.role.permissions.append(Permission(name='audit:read')); shop.db.commit()
    api = client(shop, shop.user)
    api.app.include_router(audit.router, prefix='/audit')
    entry = api.get('/audit').json()[0]
    assert (entry['action'], entry['entity_type'], entry['entity_id']) == ('DELETE', 'PRODUCT', '7')
    assert entry['ip'] == '181.115.10.20' and entry['user_id'] == shop.user.id
    assert entry['user_email'] == shop.user.email and entry['details'] == {'nombre': 'Camisa'}


def test_wrong_key_cannot_read(shop, monkeypatch):
    log_audit(shop.db, shop.user.id, 'UPDATE', 'BRANCH', '1', {'x': 1}, tenant_id=shop.tenant.id)
    raw = shop.db.query(AuditLog).order_by(AuditLog.id.desc()).first()
    monkeypatch.setattr(audit_crypto.settings, 'AUDIT_LOG_KEY', 'Gz3Jx8m2pQ0rT6wV9yB4cE7hK1nM5sU8xA2dF6gJ0kM=')
    assert audit_crypto.open_row(raw)['action'] == 'ILEGIBLE'


def test_client_ip_prefers_forwarded_header():
    scope = {'headers': [(b'x-forwarded-for', b'200.87.1.2, 10.0.0.1')], 'client': ('127.0.0.1', 5000)}
    assert client_ip(scope) == '200.87.1.2'
    assert client_ip({'headers': [], 'client': ('10.1.1.1', 1)}) == '10.1.1.1'
