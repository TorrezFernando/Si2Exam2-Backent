"""Exercise real FastAPI dependencies with disposable tenants, no demo logins."""
from datetime import date
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_sync_integrity import shop, sale
from api.deps import get_db, get_current_active_user, get_current_user_optional
from api.routers import branches, sync, analytics, products
from db.models import Branch, Permission, Inventory, Order, SyncOperation


@pytest.fixture
def client(shop):
    shop.user.role.permissions.extend([Permission(name='branches:write'), Permission(name='sales:read')])
    shop.tenant.active_subscription.plan.max_branches=2
    shop.tenant.active_subscription.plan.has_ai_reports=False
    shop.db.commit()
    app=FastAPI()
    for router,prefix in [(branches.router,'/branches'),(sync.router,'/sync'),(analytics.router,'/analytics'),(products.router,'/products')]:
        app.include_router(router,prefix=prefix)
    app.dependency_overrides[get_db]=lambda:shop.db
    app.dependency_overrides[get_current_active_user]=lambda:shop.user
    app.dependency_overrides[get_current_user_optional]=lambda:shop.user
    with TestClient(app) as http:
        yield http


def test_basic_plan_blocks_third_branch_and_ai(client,shop):
    # Existing branch is #1; creating #2 succeeds, #3 is rejected by the dependency.
    assert client.post('/branches',json={'name':'Second','address':'Test'}).status_code==201
    response=client.post('/branches',json={'name':'Third','address':'Test'})
    assert response.status_code==403
    assert shop.db.query(Branch).filter_by(tenant_id=shop.tenant.id).count()==2
    payload={'kind':'top_products','start':date.today().isoformat(),'end':date.today().isoformat()}
    assert client.post('/analytics/report',json=payload).status_code==403
    assert client.post('/analytics/interpret',json={'prompt':'ventas de este mes'}).status_code==403
    assert client.post('/analytics/ml',json={'demo':True}).status_code==403
    assert client.post('/analytics/export/xlsx',json=payload).status_code==403


def test_cross_tenant_read_update_and_sync_denied(client,shop):
    other=Branch(tenant_id=shop.foreign.tenant_id,name='Foreign',address='Test')
    shop.db.add(other);shop.db.commit();other_id=other.id
    assert client.get(f'/branches/{other_id}').status_code==404
    assert client.patch(f'/branches/{other_id}',json={'name':'Hacked'}).status_code==404
    assert client.delete(f'/branches/{other_id}').status_code==404
    assert client.get(f'/sync/catalog?branch_id={other_id}').status_code==403
    assert client.post('/sync/operations',json={'branch_id':other_id,'sales':[sale(shop)]}).status_code==403
    shop.db.expire_all()
    assert shop.db.get(Branch,other_id).name=='Foreign'


def test_basic_plan_blocks_reactivation_above_limit(client, shop):
    second = Branch(tenant_id=shop.tenant.id, name='Second', address='Test')
    dormant = Branch(tenant_id=shop.tenant.id, name='Dormant', address='Test', is_active=False)
    shop.db.add_all([second, dormant]); shop.db.commit()
    assert client.patch(f'/branches/{dormant.id}', json={'is_active': True}).status_code == 403
    shop.db.refresh(dormant)
    assert dormant.is_active is False


def test_http_duplicate_and_stock_conflict(client,shop):
    body={'branch_id':shop.branch.id,'sales':[sale(shop)]}
    first=client.post('/sync/operations',json=body)
    assert first.status_code==200
    assert client.post('/sync/operations',json=body).json()==first.json()
    assert shop.db.query(Inventory).one().stock==3
    assert shop.db.query(Order).count()==1
    conflict=client.post('/sync/operations',json={'branch_id':shop.branch.id,'sales':[sale(shop,uid='phase-six-conflict',qty=100)]})
    assert conflict.status_code==200
    assert conflict.json()['receipts'][0]['status']=='RECHAZADA_CONFLICTO'
    shop.db.expire_all()
    assert shop.db.query(Inventory).one().stock==3
    assert shop.db.query(Order).count()==1
    assert shop.db.query(SyncOperation).count()==2


def test_tenant_from_body_cannot_choose_company(client,shop):
    response=client.post('/branches',json={'name':'Owned','address':'Test','tenant_id':shop.foreign.tenant_id})
    assert response.status_code==201
    assert response.json()['tenant_id']==shop.tenant.id
    payload={'kind':'top_products','start':date.today().isoformat(),'end':date.today().isoformat(),'tenant_id':shop.foreign.tenant_id}
    assert client.post('/analytics/report',json=payload).status_code==422


def test_real_jwt_identity_and_signature(shop):
    from api.deps import get_current_user
    from core.security import create_access_token
    from fastapi import HTTPException
    from datetime import timedelta
    token=create_access_token(shop.user.id,tenant_id=shop.tenant.id,role='administrador',plan_code='basic',plan_id=shop.tenant.active_subscription.plan_id)
    assert get_current_user(shop.db,token).tenant_id==shop.tenant.id
    parts=token.split('.');parts[1]=parts[1][:-1]+('A' if parts[1][-1]!='A' else 'B')
    with pytest.raises(HTTPException) as error:get_current_user(shop.db,'.'.join(parts))
    assert error.value.status_code==401
    expired=create_access_token(shop.user.id,expires_delta=timedelta(seconds=-5))
    with pytest.raises(HTTPException) as error:get_current_user(shop.db,expired)
    assert error.value.status_code==401
