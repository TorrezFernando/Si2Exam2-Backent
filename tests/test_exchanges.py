from datetime import datetime, timedelta, timezone
import pytest
from fastapi import HTTPException
from test_commerce import shop, sale, stock
from api.routers.orders import exchange_garment, quote_exchange, return_order
from schemas.sale import GarmentExchangeCreate, GarmentExchangeQuoteRequest
from db.models.sale import GarmentExchange
from db.models.branch import Inventory


def prepare(shop, monkeypatch, elapsed=timedelta(hours=1)):
    now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
    monkeypatch.setattr('core.exchanges.utc_now', lambda: now)
    order = sale(shop)
    order.created_at = now - elapsed
    shop.db.commit()
    return order


def payload(shop, **changes):
    return GarmentExchangeCreate(**dict(dict(original_variant_id=shop.a.id, new_variant_id=shop.b.id,
        branch_id=shop.branch.id, quantity=1, request_key='test-exchange-0001',
        confirmed_difference_amount=30, settlement_confirmed=True), **changes))


@pytest.mark.parametrize('elapsed,allowed', [(timedelta(hours=47, minutes=59), True),
    (timedelta(hours=48), True), (timedelta(hours=48, seconds=1), False)])
def test_exact_48_hour_boundary(shop, monkeypatch, elapsed, allowed):
    order = prepare(shop, monkeypatch, elapsed)
    if allowed:
        assert exchange_garment(order.id, payload(shop), shop.db, shop.cashier).difference_amount == 30
    else:
        with pytest.raises(HTTPException) as e:
            exchange_garment(order.id, payload(shop), shop.db, shop.cashier)
        assert e.value.status_code == 400
        assert stock(shop) == 3 and stock(shop, shop.b.id) == 1


def test_paid_price_and_retry_after_deadline_are_stable(shop, monkeypatch):
    order = prepare(shop, monkeypatch)
    shop.product.base_price = 999; shop.db.commit()
    data = payload(shop)
    quoted = quote_exchange(order.id, GarmentExchangeQuoteRequest(**data.model_dump()), shop.db, shop.cashier)
    assert quoted['original_unit_price'] == 120 and quoted['difference_amount'] == 30
    first = exchange_garment(order.id, data, shop.db, shop.cashier)
    monkeypatch.setattr('core.exchanges.utc_now', lambda: datetime(2027, 1, 1, tzinfo=timezone.utc))
    second = exchange_garment(order.id, data, shop.db, shop.cashier)
    assert first.id == second.id
    assert shop.db.query(GarmentExchange).count() == 1
    assert stock(shop) == 4 and stock(shop, shop.b.id) == 0


@pytest.mark.parametrize('price,expected,method', [(90, -30, 'cash'), (120, 0, 'none'), (150, 30, 'cash')])
def test_cash_collect_refund_and_no_difference(shop, monkeypatch, price, expected, method):
    order = prepare(shop, monkeypatch)
    shop.b.price_override = price; shop.db.commit()
    result = exchange_garment(order.id, payload(shop, confirmed_difference_amount=expected), shop.db, shop.cashier)
    assert result.difference_amount == expected and result.settlement_method == method


@pytest.mark.parametrize('changes', [{'settlement_confirmed': False}, {'confirmed_difference_amount': 1},
    {'quantity': 3, 'confirmed_difference_amount': 90}, {'branch_id': 999}, {'new_variant_id': 999}])
def test_invalid_exchange_does_not_change_inventory(shop, monkeypatch, changes):
    order = prepare(shop, monkeypatch)
    with pytest.raises(HTTPException): exchange_garment(order.id, payload(shop, **changes), shop.db, shop.cashier)
    assert stock(shop) == 3 and stock(shop, shop.b.id) == 1
    assert shop.db.query(GarmentExchange).count() == 0


def test_reused_key_with_changed_body_is_rejected(shop, monkeypatch):
    order = prepare(shop, monkeypatch)
    exchange_garment(order.id, payload(shop), shop.db, shop.cashier)
    with pytest.raises(HTTPException) as e:
        exchange_garment(order.id, payload(shop, quantity=2), shop.db, shop.cashier)
    assert e.value.status_code == 409
    assert stock(shop) == 4


def test_exchange_blocks_full_return_of_original_items(shop, monkeypatch):
    order = prepare(shop, monkeypatch)
    exchange_garment(order.id, payload(shop), shop.db, shop.cashier)
    with pytest.raises(HTTPException) as e: return_order(order.id, shop.db, shop.cashier)
    assert e.value.status_code == 409
    assert stock(shop) == 4 and stock(shop, shop.b.id) == 0


def test_stock_conflict_rolls_back_return_leg(shop, monkeypatch):
    order = prepare(shop, monkeypatch)
    import api.routers.orders as routes
    real_move = routes.move_stock
    def fail_second(db, branch_id, variant_id, delta, *args):
        if delta < 0: raise HTTPException(409, 'Concurrent stock change')
        return real_move(db, branch_id, variant_id, delta, *args)
    monkeypatch.setattr(routes, 'move_stock', fail_second)
    with pytest.raises(HTTPException): exchange_garment(order.id, payload(shop), shop.db, shop.cashier)
    assert stock(shop) == 3 and stock(shop, shop.b.id) == 1


def test_original_product_can_be_inactive(shop, monkeypatch):
    order = prepare(shop, monkeypatch)
    from db.models import Product, ProductVariant
    new_product = Product(name='New', base_price=150, tenant_id=shop.cashier.tenant_id)
    new_variant = ProductVariant(product=new_product, tenant_id=shop.cashier.tenant_id, size='L', color='Red', sku='EXCHANGE-NEW')
    shop.db.add(new_variant); shop.db.flush()
    shop.db.add(Inventory(tenant_id=shop.cashier.tenant_id, branch_id=shop.branch.id, variant_id=new_variant.id, stock=1))
    shop.product.is_active = False; shop.db.commit()
    assert exchange_garment(order.id, payload(shop, new_variant_id=new_variant.id), shop.db, shop.cashier).difference_amount == 30
