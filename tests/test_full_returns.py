import pytest
from fastapi import HTTPException
from test_exchanges import shop, prepare, payload, stock
from api.routers.orders import exchange_garment, return_order
from api.routers.full_returns import create, preview, existing, ReturnInput
from db.models.full_return import FullReturn
from db.models.operations import InventoryMovement


def cash_sale(shop,monkeypatch):
    order=prepare(shop,monkeypatch)
    order.payment_method='efectivo';shop.db.commit()
    return order


def body(amount=270,**extra):
    return ReturnInput(**dict(dict(request_key='full-return-test-001',confirmed_refund_amount=amount,settlement_confirmed=True),**extra))


@pytest.mark.parametrize('price,refund',[(90,210),(120,240),(150,270)])
def test_refund_after_exchange_correct_holdings_amount_and_retry(shop,monkeypatch,price,refund):
    order=cash_sale(shop,monkeypatch)
    shop.b.price_override=price;shop.db.commit()
    exchange_garment(order.id,payload(shop,confirmed_difference_amount=price-120),shop.db,shop.cashier)
    quoted=preview(order.id,shop.db,shop.cashier)
    assert quoted['refund_amount']==refund
    assert {(r['variant_id'],r['quantity']) for r in quoted['items']}=={(shop.a.id,1),(shop.b.id,1)}
    shop.product.base_price=999;shop.b.price_override=888;shop.product.is_active=False;shop.db.commit()
    first=create(order.id,body(refund),shop.db,shop.cashier)
    assert first['refund_amount']==refund
    assert stock(shop)==5 and stock(shop,shop.b.id)==1
    assert create(order.id,body(refund),shop.db,shop.cashier)['id']==first['id']
    assert existing(order.id,shop.db,shop.cashier)['id']==first['id']
    assert shop.db.query(FullReturn).count()==1
    assert shop.db.query(InventoryMovement).filter_by(kind='full_return').count()==2
    assert return_order(order.id,shop.db,shop.cashier).payment_status=='refunded'
    assert stock(shop)==5


def test_rollback_after_first_inventory_movement(shop,monkeypatch):
    order=cash_sale(shop,monkeypatch)
    exchange_garment(order.id,payload(shop),shop.db,shop.cashier)
    from api.routers import full_returns
    original=full_returns.move_stock
    def fail(*args,**kwargs):
        original(*args,**kwargs)
        raise HTTPException(409,'Injected failure')
    monkeypatch.setattr(full_returns,'move_stock',fail)
    with pytest.raises(HTTPException):create(order.id,body(),shop.db,shop.cashier)
    assert stock(shop)==4 and stock(shop,shop.b.id)==0
    assert shop.db.query(FullReturn).count()==0
    assert order.is_paid


@pytest.mark.parametrize('change',['digital','unverified','wrong_amount','unconfirmed','other_branch','other_company'])
def test_reject_unsafe_returns(shop,monkeypatch,change):
    order=cash_sale(shop,monkeypatch)
    exchange=exchange_garment(order.id,payload(shop),shop.db,shop.cashier)
    data=body()
    if change=='digital':order.payment_method='tarjeta'
    if change=='unverified':exchange.settlement_method=None
    if change=='wrong_amount':data=body(1)
    if change=='unconfirmed':data=body(settlement_confirmed=False)
    if change=='other_branch':shop.cashier.branch_id=shop.other.id
    if change=='other_company':order.tenant_id=None
    shop.db.commit()
    with pytest.raises(HTTPException):create(order.id,data,shop.db,shop.cashier)
    assert shop.db.query(FullReturn).count()==0
    assert stock(shop)==4 and stock(shop,shop.b.id)==0


def test_second_key_cannot_refund_again_and_offline_cash_supported(shop,monkeypatch):
    order=cash_sale(shop,monkeypatch);order.payment_provider='Caja_Offline';shop.db.commit()
    create(order.id,body(240),shop.db,shop.cashier)
    with pytest.raises(HTTPException):create(order.id,body(240,request_key='different-return-key'),shop.db,shop.cashier)
    assert stock(shop)==5


def test_refund_report_and_inventory_after_exchange_return(shop,monkeypatch):
    from db.models import Permission
    from core.exchange_reporting import full_return_summary
    from api.routers.inventory_reports import snapshot
    order=cash_sale(shop,monkeypatch)
    shop.cashier.role.permissions.append(Permission(name='sales:read'));shop.db.commit()
    exchange_garment(order.id,payload(shop),shop.db,shop.cashier)
    create(order.id,body(),shop.db,shop.cashier)
    assert full_return_summary(shop.db,shop.cashier)==dict(count=1,refunded=270)
    assert full_return_summary(shop.db,shop.cashier,payment_method='tarjeta')==dict(count=0,refunded=0)
    report=snapshot(shop.db,shop.cashier)
    assert report['totals']['sold_net']==0
    assert report['totals']['available']==6
