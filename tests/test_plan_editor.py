"""Superadmin plan editor: editable limits, AI features/models and their enforcement."""
import pytest
from fastapi import HTTPException
from test_sync_integrity import shop
from test_security_regressions import client
from core.config import settings
from core.plan_features import current_plan, enforce_limit, text_model, image_model
from db.models import Permission, Role, SubscriptionPlan, User


@pytest.fixture
def admin(shop):
    root = User(email='root@example.invalid', hashed_password='unused', role=Role(name='superadmin'), tenant_id=None)
    shop.db.add(root); shop.db.commit()
    return client(shop, root)


def plan_of(shop):
    return shop.db.query(SubscriptionPlan).filter_by(code='test').one()


def test_superadmin_edits_limits_ai_features_and_models(shop, admin):
    plan = plan_of(shop)
    response = admin.patch(f'/saas/plans/{plan.id}', json={
        'max_users': 3, 'max_products': 10, 'max_branches': 4, 'has_ai_tryon': False,
        'has_ai_reports': True, 'ai_text_model': 'gemini-2.5-pro', 'description': 'Plan editado', 'price': 49.5})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body['max_users'], body['max_products'], body['max_branches']) == (3, 10, 4)
    assert body['has_ai_tryon'] is False and body['has_ai_shopper'] is True and body['has_ai_reports'] is True
    assert body['ai_text_model'] == 'gemini-2.5-pro' and body['subscribers_count'] == 1
    shop.db.expire_all()
    assert text_model(plan_of(shop)) == 'gemini-2.5-pro'
    assert image_model(plan_of(shop)) == settings.GEMINI_MODEL
    # null en un límite = ilimitado; en un campo obligatorio se rechaza
    assert admin.patch(f'/saas/plans/{plan.id}', json={'max_users': None}).json()['max_users'] is None
    assert admin.patch(f'/saas/plans/{plan.id}', json={'name': None}).status_code == 422
    assert admin.patch(f'/saas/plans/{plan.id}', json={'ai_text_model': 'gpt-4; drop'}).status_code == 422
    assert admin.patch(f'/saas/plans/{plan.id}', json={'is_active': False}).status_code == 409
    assert admin.get('/saas/plans/ai-models').json()['text_models']


def test_only_superadmin_can_edit_or_create_plans(shop):
    company_admin = client(shop, shop.user)
    plan = plan_of(shop)
    assert company_admin.patch(f'/saas/plans/{plan.id}', json={'max_branches': 99}).status_code == 403
    assert company_admin.post('/saas/plans', json={'code': 'x1', 'name': 'X', 'price': 1, 'max_branches': 1}).status_code == 403


def test_create_plan_and_list_inactive(shop, admin):
    created = admin.post('/saas/plans', json={'code': 'starter', 'name': 'Starter', 'price': 9.99, 'max_branches': 1,
                                              'max_users': 2, 'has_ai_shopper': False, 'is_active': False})
    assert created.status_code == 201, created.text
    assert created.json()['has_ai_shopper'] is False and created.json()['has_ai_tryon'] is True
    assert admin.post('/saas/plans', json={'code': 'starter', 'name': 'Dup', 'price': 1, 'max_branches': 1}).status_code == 409
    assert 'starter' not in {p['code'] for p in admin.get('/saas/plans').json()}
    assert 'starter' in {p['code'] for p in admin.get('/saas/plans?include_inactive=true').json()}


def test_user_and_product_limits_are_enforced(shop):
    plan = plan_of(shop)
    plan.max_users = 1; plan.max_products = 1; shop.db.commit()
    # staff actual: 1 (el cliente no cuenta)
    with pytest.raises(HTTPException) as error:
        enforce_limit(shop.db, shop.tenant.id, 'users')
    assert error.value.status_code == 403
    shop.user.role.permissions.append(Permission(name='catalog:write')); shop.db.commit()
    response = client(shop, shop.user).post('/products', json={'name': 'Nueva', 'base_price': 50})
    assert response.status_code == 403 and 'Límite de productos' in response.json()['detail']
    plan.max_products = None; shop.db.commit()
    assert client(shop, shop.user).post('/products', json={'name': 'Nueva', 'base_price': 50}).status_code == 201


def test_ai_features_follow_the_company_plan(shop):
    plan = current_plan(shop.db, shop.tenant.id)
    assert plan.has_ai_shopper and plan.has_ai_tryon  # comportamiento previo conservado
    plan.has_ai_tryon = False; plan.ai_image_model = 'gemini-2.5-flash-image'; shop.db.commit()
    assert current_plan(shop.db, shop.tenant.id).has_ai_tryon is False
    assert image_model(plan) == 'gemini-2.5-flash-image'
