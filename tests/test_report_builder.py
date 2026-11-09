"""Constructor de reportes: columnas, criterios y orden elegidos por el usuario; aislado por empresa."""
import pytest
from test_sync_integrity import shop
from test_security_regressions import client
from api.routers import report_builder
from core import report_builder as rb
from db.models import Order, Permission, Product, Tenant


def api_for(shop, *perms):
    for p in perms:
        shop.user.role.permissions.append(Permission(name=p))
    shop.db.commit()
    api = client(shop, shop.user)
    api.app.include_router(report_builder.router, prefix='/report-builder')
    return api


def add_orders(shop):
    for total in (50, 300, 120):
        shop.db.add(Order(tenant_id=shop.tenant.id, user_id=shop.customer.id, branch_id=shop.branch.id,
                          total_amount=total, is_paid=True, payment_status='paid', payment_method='efectivo',
                          order_type='pos_presencial'))
    other = shop.db.query(Tenant).filter_by(subdomain='other').one()
    shop.db.add(Order(tenant_id=other.id, user_id=shop.foreign.id, branch_id=shop.branch.id, total_amount=999,
                      is_paid=True, payment_status='paid', payment_method='efectivo', order_type='pos_presencial'))
    shop.db.commit()


def test_user_chooses_columns_criteria_and_order(shop):
    add_orders(shop)
    api = api_for(shop, 'sales:read', 'branches:write')
    sources = {s['key'] for s in api.get('/report-builder/metadata').json()}
    assert 'ventas' in sources and 'inventario' not in sources  # sin permiso de inventario
    spec = {'source': 'ventas', 'columns': ['total', 'sucursal'],
            'filters': [{'column': 'total', 'op': 'gte', 'value': 100}],
            'order_by': [{'column': 'total', 'direction': 'desc'}]}
    report = api.post('/report-builder/run', json=spec).json()
    assert [c['key'] for c in report['columns']] == ['total', 'sucursal']
    assert [r[0] for r in report['rows']] == [300, 120]  # filtrado, ordenado y sin la venta de la otra empresa
    assert report['filters'] == ['Total (Bs) mayor o igual 100']


@pytest.mark.parametrize('fmt,marker', [('xlsx', b'PK'), ('pdf', b'%PDF'), ('html', b'<!doctype html>'), ('csv', b'Total')])
def test_exports_all_formats(shop, fmt, marker):
    add_orders(shop)
    api = api_for(shop, 'sales:read', 'branches:write')
    response = api.post(f'/report-builder/export/{fmt}', json={'source': 'ventas', 'columns': ['id', 'total', 'cliente']})
    assert response.status_code == 200 and marker in response.content[:600]


def test_rejects_unknown_columns_and_unauthorized_sources(shop):
    api = api_for(shop, 'sales:read')
    assert api.post('/report-builder/run', json={'source': 'ventas', 'columns': ['hashed_password']}).status_code == 422
    assert api.post('/report-builder/run', json={'source': 'inventario', 'columns': ['stock']}).status_code == 403
    bad = {'source': 'ventas', 'columns': ['total'], 'filters': [{'column': 'total', 'op': 'contains', 'value': 'x'}]}
    assert api.post('/report-builder/run', json=bad).status_code == 422


def test_single_branch_user_only_sees_own_branch(shop):
    add_orders(shop)
    api = api_for(shop, 'sales:read')
    report = api.post('/report-builder/run', json={'source': 'ventas', 'columns': ['total']}).json()
    assert 'Sucursal asignada al usuario' in report['filters'] and report['count'] == 3


def test_email_requires_smtp_configuration(shop, monkeypatch):
    api = api_for(shop, 'sales:read', 'branches:write')
    monkeypatch.setattr(rb.settings, 'SMTP_HOST', '')
    response = api.post('/report-builder/email', json={'source': 'ventas', 'columns': ['total'], 'to': ['jefe@empresa.com']})
    assert response.status_code == 503


def test_email_sends_report_with_attachment(shop, monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout): sent['host'] = host
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): sent['tls'] = True
        def login(self, user, password): sent['login'] = user
        def send_message(self, mail): sent['mail'] = mail

    monkeypatch.setattr(rb.settings, 'SMTP_HOST', 'smtp.example.com')
    monkeypatch.setattr(rb.settings, 'SMTP_FROM', 'reportes@example.com')
    monkeypatch.setattr(rb.settings, 'SMTP_USER', 'reportes@example.com')
    monkeypatch.setattr(rb.smtplib, 'SMTP', FakeSMTP)
    add_orders(shop)
    api = api_for(shop, 'sales:read', 'branches:write')
    response = api.post('/report-builder/email', json={'source': 'ventas', 'columns': ['id', 'total'], 'to': ['jefe@empresa.com'], 'format': 'xlsx'})
    assert response.status_code == 200, response.text
    attachments = [p.get_filename() for p in sent['mail'].iter_attachments()]
    assert sent['tls'] and attachments == ['reporte.xlsx'] and sent['mail']['To'] == 'jefe@empresa.com'
