from datetime import date
from test_security_regressions import shop,client
from db.models import Permission,Role,Branch,Product,ProductVariant,Inventory,Order,OrderItem,User
from db.models.operations import InventoryMovement
from tools.seed_demo_sales import generate


def test_company_role_list_hides_global_and_excess_privileges(shop):
    shop.user.role.permissions.append(Permission(name='roles:manage'))
    global_role=Role(name='superadmin')
    privileged=Role(name='platform_operator',permissions=[Permission(name='platform:manage')])
    shop.db.add_all([global_role,privileged]);shop.db.commit()
    api=client(shop,shop.user)
    names={r['name'] for r in api.get('/roles').json()}
    assert 'superadmin' not in names and 'platform_operator' not in names
    assert 'cliente' in names
    assert 'platform:manage' not in {r['name'] for r in api.get('/roles/permissions').json()}
    shop.user.role=global_role;shop.user.tenant_id=None;shop.db.commit()
    assert {'superadmin','platform_operator'} <= {r['name'] for r in api.get('/roles').json()}


def test_six_month_demo_both_tenants_repeat_and_stock_preserved(shop):
    tenant2=shop.foreign.tenant_id
    branch=Branch(tenant_id=tenant2,name='Second company',address='Demo')
    product=Product(tenant_id=tenant2,name='Other shirt',base_price=50)
    variant=ProductVariant(tenant_id=tenant2,product=product,size='S',color='Red',sku='DEMO-SECOND')
    shop.db.add_all([branch,product,variant]);shop.db.flush()
    shop.db.add(Inventory(tenant_id=tenant2,branch_id=branch.id,variant_id=variant.id,stock=7));shop.db.commit()
    original=[(r.id,r.stock) for r in shop.db.query(Inventory).order_by(Inventory.id)]
    ids=[shop.tenant.id,tenant2]
    result=generate(shop.db,date(2026,11,4),ids);shop.db.commit()
    assert len(result)==2
    assert all(m['units']>0 for r in result for m in r['months'].values())
    assert all(r['added']>0 and set(r['months'])=={f'2026-{m:02d}' for m in range(5,11)} for r in result)
    for order in shop.db.query(Order):
        assert order.tenant_id==order.user.tenant_id==order.branch.tenant_id
        assert all(i.tenant_id==order.tenant_id==i.variant.tenant_id for i in order.items)
        assert round(sum(i.quantity*i.unit_price for i in order.items),2)==round(order.total_amount,2)
    again=generate(shop.db,date(2026,11,4),ids);shop.db.commit()
    assert all(r['added']==0 and r['existing']>0 for r in again)
    assert [(r.id,r.stock) for r in shop.db.query(Inventory).order_by(Inventory.id)]==original
    assert sum(r.quantity for r in shop.db.query(InventoryMovement))==0
