from datetime import datetime, timedelta, timezone
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from test_security_regressions import shop, client
from core.subscription_plans import ensure_plans, process_expired_subscriptions
from db.models import Permission, Branch, Subscription, User, Role
from api.deps import verify_branch_limit, require_feature
from api.routers import branches, reservations


def pay(api, monkeypatch, path, json=None):
    """Company plan changes/renewals go through PayPal: start checkout, approve (simulated) and verify."""
    from core import paypal
    monkeypatch.setattr(paypal, 'ensure_configured', lambda: None)
    monkeypatch.setattr(paypal, 'create_checkout', lambda ref, *a: (f'PP-{ref}', 'https://www.sandbox.paypal.com/checkoutnow?token=x'))
    monkeypatch.setattr(paypal, 'is_captured', lambda *a: False)
    started = api.post(path, json=json)
    assert started.status_code == 200, started.text
    assert started.json()['status'] == 'pending' and started.json()['payment_url'].startswith('https://')
    payment_id = started.json()['payment_id']
    # Sin pago confirmado no cambia nada
    assert api.post(f'/saas/my-subscription/payments/{payment_id}/verify').json()['status'] == 'pending'
    monkeypatch.setattr(paypal, 'is_captured', lambda *a: True)
    verified = api.post(f'/saas/my-subscription/payments/{payment_id}/verify')
    assert verified.status_code == 200, verified.text
    assert verified.json()['status'] == 'applied'
    # Reintentar la verificación no vuelve a aplicar el plan
    assert api.post(f'/saas/my-subscription/payments/{payment_id}/verify').json()['status'] == 'applied'
    return verified.json()['tenant']


def prepare(shop):
    plans = ensure_plans(shop.db)
    shop.user.role.permissions.append(Permission(name='branches:write'))
    shop.db.commit()
    return plans


def test_three_plans_preserve_existing_configuration(shop):
    plans = prepare(shop)
    plans['enterprise'].price = 210
    shop.db.commit()
    ensure_plans(shop.db)
    rows = client(shop, shop.user).get('/saas/plans').json()
    assert {'basic', 'professional', 'enterprise'} <= {p['code'] for p in rows}
    assert next(p for p in rows if p['code'] == 'enterprise')['price'] == 210


def test_enterprise_upgrade_features_limit_and_downgrade(shop, monkeypatch):
    prepare(shop)
    api = client(shop, shop.user)
    tenant = pay(api, monkeypatch, '/saas/my-subscription/change-plan', {'plan_code': 'enterprise'})
    assert tenant['active_plan']['max_branches'] == 30
    request = Request({'type': 'http', 'headers': []})
    assert require_feature('ai_reports')(request, shop.user, shop.db) == shop.user
    existing = shop.db.query(Branch).filter_by(tenant_id=shop.tenant.id, is_active=True).count()
    shop.db.add_all([Branch(tenant_id=shop.tenant.id, name=f'Branch {n}', address='Test') for n in range(29-existing)])
    shop.db.commit()
    verify_branch_limit(request, shop.user, shop.db)
    shop.db.add(Branch(tenant_id=shop.tenant.id, name='Thirtieth', address='Test'))
    shop.db.commit()
    with pytest.raises(HTTPException) as error:
        verify_branch_limit(request, shop.user, shop.db)
    assert error.value.status_code == 403
    assert api.post('/saas/my-subscription/change-plan', json={'plan_code': 'professional'}).status_code == 400
    assert shop.db.query(Subscription).filter_by(tenant_id=shop.tenant.id, status='active').count() == 1


@pytest.mark.parametrize('code', ['missing', 'enterprise'])
def test_reject_unknown_or_inactive_plan_on_company_creation(shop, code):
    plans = prepare(shop)
    plans['enterprise'].is_active = False
    shop.user.role.name = 'superadmin'
    shop.user.tenant_id = None
    shop.db.commit()
    response = client(shop, shop.user).post('/saas/tenants', json={
        'name': 'New company', 'subdomain': 'new-company', 'plan_code': code})
    assert response.status_code == 400, response.text


def test_customer_cannot_select_enterprise(shop):
    prepare(shop)
    response = client(shop, shop.customer).post('/saas/my-subscription/change-plan', json={'plan_code': 'enterprise'})
    assert response.status_code == 403


def test_monthly_subscription_auto_cancels_at_period_end_and_renews(shop, monkeypatch):
    prepare(shop)
    api = client(shop, shop.user)

    # Upgrade to professional (paid): sets 30-day end_date and cancel_at_period_end=True by default
    pay(api, monkeypatch, '/saas/my-subscription/checkout', {'kind': 'change_plan', 'plan_code': 'professional'})
    sub = shop.db.query(Subscription).filter_by(tenant_id=shop.tenant.id, status='active').one()
    assert sub.cancel_at_period_end is True
    assert sub.end_date is not None

    # Check my-subscription payload includes end_date, cancel_at_period_end, days_remaining
    my_sub = api.get('/saas/my-subscription').json()
    assert my_sub['cancel_at_period_end'] is True
    assert my_sub['end_date'] is not None
    assert my_sub['days_remaining'] >= 29

    # Toggle auto-cancel off and back on
    toggle_off = api.patch('/saas/my-subscription/auto-cancel', json={'cancel_at_period_end': False})
    assert toggle_off.status_code == 200
    assert toggle_off.json()['cancel_at_period_end'] is False
    toggle_on = api.patch('/saas/my-subscription/auto-cancel', json={'cancel_at_period_end': True})
    assert toggle_on.status_code == 200
    assert toggle_on.json()['cancel_at_period_end'] is True

    # Simulate end of monthly cycle (end_date in the past)
    sub.end_date = datetime.now(timezone.utc) - timedelta(minutes=5)
    shop.db.commit()

    cancelled_count = process_expired_subscriptions(shop.db, tenant_id=shop.tenant.id)
    assert cancelled_count == 1
    shop.db.refresh(sub)
    assert sub.status == 'cancelled'
    assert sub.cancelled_at is not None

    # Feature & branch limits must reject when subscription was auto-cancelled
    request = Request({'type': 'http', 'headers': []})
    with pytest.raises(HTTPException) as feat_err:
        require_feature('ai_reports')(request, shop.user, shop.db)
    assert feat_err.value.status_code == 403

    with pytest.raises(HTTPException) as branch_err:
        verify_branch_limit(request, shop.user, shop.db)
    assert branch_err.value.status_code == 403

    # Renew for another 30-day cycle
    renewed = pay(api, monkeypatch, '/saas/my-subscription/renew')
    assert renewed['subscription_status'] == 'active'
    assert renewed['days_remaining'] >= 29
    assert require_feature('ai_reports')(request, shop.user, shop.db) == shop.user


def test_superadmin_is_strictly_separated_from_company_modules(shop):
    prepare(shop)
    sa_role = Role(name='superadmin', description='SaaS Platform Admin', is_predefined=True)
    sa_user = User(email='superadmin@platform.test', hashed_password='x', role=sa_role, tenant_id=None, is_active=True)
    shop.db.add_all([sa_role, sa_user])
    shop.db.commit()

    api = client(shop, sa_user)
    api.app.include_router(branches.router, prefix='/branches')
    api.app.include_router(reservations.router, prefix='/reservations')

    # SaaS & global endpoints succeed for SuperAdmin
    assert api.get('/saas/tenants').status_code == 200
    assert api.get('/roles/').status_code == 200
    my_sub = api.get('/saas/my-subscription')
    assert my_sub.status_code == 200
    assert my_sub.json()['status'] == 'global'
    assert my_sub.json()['tenant'] is None

    # Company-internal operational modules MUST reject SuperAdmin with 403
    assert api.post('/products/', json={'name': 'Test', 'base_price': 10, 'variants': []}).status_code == 403
    assert api.post('/branches/', json={'name': 'Sucursal X', 'address': 'Av 1'}).status_code == 403
    assert api.get('/users/').status_code == 403
    assert api.get('/orders/').status_code == 403
    assert api.get('/operations/masters/categories').status_code == 403
    assert api.get('/reservations/').status_code == 403



def test_company_cannot_bypass_payment_with_superadmin_routes(shop):
    prepare(shop)
    api = client(shop, shop.user)
    before = shop.db.query(Subscription).filter_by(tenant_id=shop.tenant.id, status='active').one().plan_id
    assert api.post(f'/saas/tenants/{shop.tenant.id}/change-plan', json={'plan_code': 'enterprise'}).status_code == 403
    assert api.post(f'/saas/tenants/{shop.tenant.id}/renew').status_code == 403
    shop.db.expire_all()
    assert shop.db.query(Subscription).filter_by(tenant_id=shop.tenant.id, status='active').one().plan_id == before


def test_paid_plan_requires_configured_gateway(shop, monkeypatch):
    from core import paypal
    from core.config import settings
    prepare(shop)
    monkeypatch.setattr(settings, 'PAYPAL_CLIENT_ID', '')
    response = client(shop, shop.user).post('/saas/my-subscription/checkout', json={'kind': 'change_plan', 'plan_code': 'enterprise'})
    assert response.status_code == 503


def test_checkout_form_saved_offline_is_charged_once_with_billing(shop, monkeypatch):
    from core import paypal
    from db.models import SubscriptionPayment
    prepare(shop)
    monkeypatch.setattr(paypal, 'ensure_configured', lambda: None)
    created = []
    monkeypatch.setattr(paypal, 'create_checkout', lambda ref, *a: created.append(ref) or (f'PP-{ref}', 'https://www.sandbox.paypal.com/x'))
    api = client(shop, shop.user)
    form = {'kind': 'change_plan', 'plan_code': 'enterprise', 'request_key': 'plan-form-0001',
            'billing': {'name': 'Boutique SRL', 'tax_id': '1234567', 'email': 'facturas@boutique.com'}}
    first = api.post('/saas/my-subscription/checkout', json=form).json()
    again = api.post('/saas/my-subscription/checkout', json=form).json()  # reenvío al volver la conexión
    assert first['payment_id'] == again['payment_id'] and len(created) == 1
    assert first['billing'] == {'name': 'Boutique SRL', 'tax_id': '1234567', 'email': 'facturas@boutique.com'}
    assert shop.db.query(SubscriptionPayment).count() == 1
    bad = dict(form, request_key='plan-form-0002', billing={'name': 'X', 'email': 'no-es-correo'})
    assert api.post('/saas/my-subscription/checkout', json=bad).status_code == 422
