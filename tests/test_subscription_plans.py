import pytest
from fastapi import HTTPException
from starlette.requests import Request
from test_security_regressions import shop, client
from core.subscription_plans import ensure_plans
from db.models import Permission, Branch, Subscription
from api.deps import verify_branch_limit, require_feature


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


def test_enterprise_upgrade_features_limit_and_downgrade(shop):
    prepare(shop)
    api = client(shop, shop.user)
    response = api.post('/saas/my-subscription/change-plan', json={'plan_code': 'enterprise'})
    assert response.status_code == 200, response.text
    assert response.json()['active_plan']['max_branches'] == 30
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
