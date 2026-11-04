"""Security acceptance regressions; all data is disposable SQLite in memory."""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / 'Backend-Si2'
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / 'tests'))

from test_sync_integrity import shop
from fastapi import FastAPI
from fastapi.testclient import TestClient
from api import deps
from api.routers import inventory, operations, saas, users, orders, products, roles
from db.database import get_db as inventory_db
from db.models import Permission, Role, SubscriptionPlan, Branch, Product, ProductVariant, Inventory
from db.models.product import Category


def client(shop, user):
    app = FastAPI()
    for prefix, router in [('inventory', inventory.router), ('operations', operations.router),
                           ('saas', saas.router), ('users', users.router), ('orders', orders.router),
                           ('products', products.router), ('roles', roles.router)]:
        app.include_router(router, prefix='/' + prefix)
    app.dependency_overrides[deps.get_db] = lambda: shop.db
    app.dependency_overrides[inventory_db] = lambda: shop.db
    if user is not None:
        app.dependency_overrides[deps.get_current_active_user] = lambda: user
        app.dependency_overrides[deps.get_current_user_optional] = lambda: user
    return TestClient(app)


def test_private_branch_inventory_requires_authentication(shop):
    response = client(shop, None).get(f'/inventory/branch/{shop.branch.id}')
    assert response.status_code in (401, 403), response.text


def test_master_categories_do_not_leak_other_tenants(shop):
    shop.user.role.permissions.append(Permission(name='catalog:write'))
    foreign = Category(name='Foreign private category', tenant_id=shop.foreign.tenant_id)
    shop.db.add(foreign)
    shop.db.commit()
    response = client(shop, shop.user).get('/operations/masters/categories')
    assert response.status_code == 200
    assert foreign.id not in [row['id'] for row in response.json()]


def test_customer_cannot_change_company_policy(shop):
    response = client(shop, shop.customer).patch(
        f'/saas/tenants/{shop.tenant.id}', json={'exchange_window_days': 1})
    assert response.status_code == 403, response.text


def test_customer_cannot_upgrade_subscription(shop):
    shop.db.add(SubscriptionPlan(name='Professional test', code='professional', max_branches=10, has_ai_reports=True))
    shop.db.commit()
    response = client(shop, shop.customer).post(
        '/saas/my-subscription/change-plan', json={'plan_code': 'professional'})
    assert response.status_code == 403, response.text


def test_tenant_staff_cannot_assign_global_superadmin(shop):
    shop.customer.email = 'customer@example.com'
    shop.user.role.permissions.append(Permission(name='users:write'))
    global_role = Role(name='superadmin')
    shop.db.add(global_role)
    shop.db.commit()
    response = client(shop, shop.user).patch(
        f'/users/{shop.customer.id}', json={'role_id': global_role.id})
    assert response.status_code in (400, 403, 422), response.text


def test_store_admin_cannot_sell_stock_of_another_tenant(shop):
    shop.user.email = 'staff@example.com'
    shop.user.role.permissions.append(Permission(name='branches:write'))
    foreign_branch = Branch(name='Foreign', address='Other', tenant_id=shop.foreign.tenant_id)
    foreign_product = Product(name='Foreign product', base_price=50, tenant_id=shop.foreign.tenant_id)
    foreign_variant = ProductVariant(product=foreign_product, tenant_id=shop.foreign.tenant_id,
                                     size='S', color='Red', sku='FOREIGN-AUDIT')
    shop.db.add_all([foreign_branch, foreign_variant])
    shop.db.flush()
    stock = Inventory(branch_id=foreign_branch.id, variant_id=foreign_variant.id,
                      tenant_id=shop.foreign.tenant_id, stock=5)
    shop.db.add(stock)
    shop.db.commit()
    response = client(shop, shop.user).post('/orders', json={
        'branch_id': foreign_branch.id, 'order_type': 'presencial', 'payment_method': 'efectivo',
        'items': [{'variant_id': foreign_variant.id, 'quantity': 1}]})
    assert response.status_code in (400, 403, 404), response.text
    shop.db.refresh(stock)
    assert stock.stock == 5


def test_public_inventory_requires_selected_store_and_obeys_it(shop):
    http = client(shop, None)
    assert http.get(f'/inventory/variant/{shop.variant.id}').status_code == 403
    assert http.get(f'/inventory/variant/{shop.variant.id}?store=test').json() == [{'branch_id': shop.branch.id, 'stock': 5}]
    assert http.get(f'/inventory/variant/{shop.variant.id}?store=other').json() == []


def test_foreign_product_cannot_be_modified_or_deleted(shop):
    shop.user.role.permissions.append(Permission(name='catalog:write'))
    product = Product(name='Foreign', base_price=50, tenant_id=shop.foreign.tenant_id)
    shop.db.add(product); shop.db.commit()
    http = client(shop, shop.user)
    assert http.patch(f'/products/{product.id}', json={'name': 'Changed'}).status_code == 404
    assert http.delete(f'/products/{product.id}').status_code == 404
    shop.db.refresh(product)
    assert product.name == 'Foreign' and product.is_active


def test_product_creation_assigns_tenant_to_entire_stock_graph(shop):
    shop.user.role.permissions.extend([Permission(name='catalog:write'), Permission(name='branches:write')])
    other_branch = Branch(name='Other', address='Other', tenant_id=shop.foreign.tenant_id)
    shop.db.add(other_branch); shop.db.commit()
    response = client(shop, shop.user).post('/products', json={'name': 'New', 'base_price': 50,
        'variants': [{'size': 'L', 'color': 'White', 'sku': 'NEW-AUDIT', 'quantity': 2}]})
    assert response.status_code == 201, response.text
    product = shop.db.get(Product, response.json()['id'])
    assert product.tenant_id == shop.tenant.id
    for variant in product.variants:
        assert variant.tenant_id == shop.tenant.id
        assert len(variant.inventories) == 1
        assert variant.inventories[0].tenant_id == shop.tenant.id
        assert variant.inventories[0].branch_id == shop.branch.id


def test_staff_cannot_edit_shared_roles_or_foreign_users(shop):
    shop.user.role.permissions.extend([Permission(name='roles:manage'), Permission(name='users:write')])
    shop.db.commit()
    http = client(shop, shop.user)
    assert http.patch(f'/roles/{shop.customer.role_id}', json={'name': 'superadmin'}).status_code == 403
    assert http.delete(f'/users/{shop.foreign.id}').status_code == 404
    permission_id = shop.user.role.permissions[0].id
    assert http.post(f'/users/{shop.foreign.id}/permissions?permission_id={permission_id}&is_granted=true').status_code == 404


def test_company_admin_can_update_own_policy(shop):
    shop.user.role.permissions.append(Permission(name='branches:write'))
    shop.db.commit()
    response = client(shop, shop.user).patch(f'/saas/tenants/{shop.tenant.id}', json={'exchange_window_days': 2})
    assert response.status_code == 200
    assert response.json()['exchange_window_days'] == 2
