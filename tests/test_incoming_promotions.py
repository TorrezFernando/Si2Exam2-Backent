from datetime import datetime, timedelta, timezone
import pytest
from test_security_regressions import shop, client
from db.models import Permission, Inventory, InventoryMovement, Order
from db.models.operations import Supplier
from db.models.incoming import IncomingStock, IncomingReceipt
from core.pricing import effective
from core.commerce import price_variant
from api.routers.sync import get_offline_catalog


def setup(shop):
    shop.user.role.permissions.extend([Permission(name='inventory:write'), Permission(name='catalog:write')])
    supplier=Supplier(name='Proveedor', tenant_id=shop.tenant.id)
    shop.db.add(supplier); shop.db.commit()
    return client(shop,shop.user), supplier


def planned(shop, supplier, **kwargs):
    return dict(branch_id=shop.branch.id, supplier_id=supplier.id, variant_id=shop.variant.id,
        quantity=10, expected_date=(datetime.now(timezone.utc)+timedelta(days=5)).date().isoformat(),
        reference='Pedido 001', request_key='planned-request-001', **kwargs)


def test_partial_receipt_retry_complete_and_cancel(shop):
    api,supplier=setup(shop); payload=planned(shop,supplier)
    res=api.post('/operations/incoming',json=payload)
    assert res.status_code==201,res.text
    row=res.json(); id=row['id']
    assert row['pending_quantity']==10
    assert api.post('/operations/incoming',json=payload).json()['id']==id
    assert shop.db.query(Inventory).one().stock==5
    body={'quantity':4,'request_key':'receipt-request-001'}
    first=api.post(f'/operations/incoming/{id}/receive',json=body)
    assert first.status_code==200,first.text
    assert first.json()['incoming']['pending_quantity']==6
    assert api.post(f'/operations/incoming/{id}/receive',json=body).json()['receipt_id']==first.json()['receipt_id']
    shop.db.expire_all()
    assert shop.db.query(Inventory).one().stock==9
    assert shop.db.query(IncomingReceipt).count()==1
    assert api.post(f'/operations/incoming/{id}/receive',json={**body,'quantity':5}).status_code==409
    assert api.post(f'/operations/incoming/{id}/receive',json={'quantity':7,'request_key':'receipt-over-001'}).status_code==409
    assert api.post(f'/operations/incoming/{id}/cancel').json()['pending_quantity']==0
    assert api.post(f'/operations/incoming/{id}/receive',json={'quantity':1,'request_key':'receipt-after-cancel'}).status_code==409
    assert shop.db.query(InventoryMovement).count()==1


def test_receipt_failure_rolls_back_all_and_completion(shop,monkeypatch):
    api,supplier=setup(shop)
    id=api.post('/operations/incoming',json=planned(shop,supplier)).json()['id']
    from api.routers import incoming
    original=incoming.move_stock
    def fail(*args,**kwargs):
        original(*args,**kwargs)
        from fastapi import HTTPException
        raise HTTPException(409,'Injected failure')
    monkeypatch.setattr(incoming,'move_stock',fail)
    body={'quantity':10,'request_key':'receipt-complete-001'}
    assert api.post(f'/operations/incoming/{id}/receive',json=body).status_code==409
    shop.db.expire_all()
    assert shop.db.query(Inventory).one().stock==5
    assert shop.db.query(IncomingStock).one().received_quantity==0
    assert shop.db.query(IncomingReceipt).count()==0
    monkeypatch.setattr(incoming,'move_stock',original)
    assert api.post(f'/operations/incoming/{id}/receive',json=body).json()['incoming']['status']=='received'
    assert api.post(f'/operations/incoming/{id}/cancel').status_code==409


def test_incoming_authorization_and_foreign_supplier(shop):
    api,supplier=setup(shop)
    data=planned(shop,supplier)
    assert client(shop,shop.customer).post('/operations/incoming',json=data).status_code==403
    supplier.tenant_id=shop.foreign.tenant_id;shop.db.commit()
    assert api.post('/operations/incoming',json=data).status_code==404
    assert shop.db.query(IncomingStock).count()==0


def test_promotion_price_window_variant_sales_and_offline(shop):
    api,_=setup(shop)
    now=datetime.now(timezone.utc)
    body={'promotion_percent':25,'promotion_start':(now-timedelta(hours=1)).isoformat(),'promotion_end':(now+timedelta(hours=1)).isoformat()}
    res=api.patch(f'/products/{shop.variant.product_id}',json=body)
    assert res.status_code==200,res.text
    assert res.json()['effective_price']==90
    assert res.json()['variants'][0]['effective_price']==90
    product=shop.variant.product
    assert effective(product,120,now-timedelta(hours=2))==120
    assert effective(product,120,now+timedelta(hours=1))==120
    assert effective(product,120,now-timedelta(hours=1))==90
    shop.variant.price_override=200;shop.db.commit()
    assert price_variant(shop.db,shop.variant.id,shop.tenant.id)[1]==150
    cat=get_offline_catalog(shop.branch.id,None,shop.db,shop.user)
    assert cat.products[0].variants[0].price==150
    response=api.post('/orders',json={'branch_id':shop.branch.id,'items':[{'variant_id':shop.variant.id,'quantity':1,'unit_price':1}]})
    assert response.status_code==201,response.text
    assert response.json()['total_amount']==150
    clear=api.patch(f'/products/{product.id}',json={'promotion_percent':None,'promotion_start':None,'promotion_end':None})
    assert clear.status_code==200
    assert price_variant(shop.db,shop.variant.id,shop.tenant.id)[1]==200
    assert shop.db.query(Order).one().total_amount==150


@pytest.mark.parametrize('fields',[
    {'promotion_percent':-1}, {'promotion_percent':101}, {'promotion_percent':10},
    {'promotion_percent':10,'promotion_start':'2026-10-30T12:00:00Z','promotion_end':'2026-10-29T12:00:00Z'},
    {'promotion_percent':10,'promotion_start':'2026-10-30T12:00:00','promotion_end':'2026-10-31T12:00:00'},
])
def test_invalid_promotions_rejected(shop,fields):
    api,_=setup(shop)
    assert api.patch(f'/products/{shop.variant.product_id}',json=fields).status_code==422


def test_expired_promotion_offline_cash_is_not_silently_repriced(shop):
    from test_sync_integrity import sale, send
    product=shop.variant.product
    now=datetime.now(timezone.utc)
    product.promotion_percent=25
    product.promotion_start=now-timedelta(days=2)
    product.promotion_end=now-timedelta(days=1)
    shop.db.commit()
    response=send(shop,sales=[sale(shop,price=90)])
    assert response.receipts[0].status=='RECHAZADA_CONFLICTO'
    assert shop.db.query(Order).count()==0
    assert shop.db.query(Inventory).one().stock==5


def test_utc_offset_normalized_when_creating_promotion(shop):
    api,_=setup(shop)
    response=api.post('/products',json={'name':'Timed offer','base_price':100,
        'promotion_percent':10,'promotion_start':'2030-01-01T08:00:00-04:00',
        'promotion_end':'2030-01-01T09:00:00-04:00'})
    assert response.status_code==201,response.text
    assert response.json()['promotion_start']=='2030-01-01T12:00:00Z'
