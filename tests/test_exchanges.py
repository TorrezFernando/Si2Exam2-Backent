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


def test_defective_same_product_exchange_and_additional_payment_when_higher_price(shop, monkeypatch):
    order = prepare(shop, monkeypatch)
    # 1) Cambio por el mismo producto (defectuoso): sin diferencia de precio y la prenda defectuosa no infla el stock vendible
    quoted_same = quote_exchange(
        order.id,
        GarmentExchangeQuoteRequest(
            original_variant_id=shop.a.id,
            new_variant_id=shop.a.id,
            branch_id=shop.branch.id,
            quantity=1,
            reason='Prenda defectuosa - reposición mismo producto',
            exchange_type='same_defective',
        ),
        shop.db,
        shop.cashier,
    )
    assert quoted_same['difference_amount'] == 0
    assert quoted_same['exchange_type'] == 'same_defective'
    same_res = exchange_garment(
        order.id,
        payload(
            shop,
            original_variant_id=shop.a.id,
            new_variant_id=shop.a.id,
            confirmed_difference_amount=0,
            exchange_type='same_defective',
            reason='Prenda defectuosa - reposición mismo producto',
            request_key='test-defective-same-0001',
        ),
        shop.db,
        shop.cashier,
    )
    assert same_res.exchange_type == 'same_defective'
    assert same_res.difference_amount == 0
    # Se entregó 1 unidad buena de shop.a (stock baja de 3 a 2) y la defectuosa quedó como merma sin reingresar a stock vendible
    assert stock(shop) == 2

    # 2) Si cambia por otro producto que supera el precio, registra el dinero adicional aumentado y el método de pago
    higher_res = exchange_garment(
        order.id,
        payload(
            shop,
            original_variant_id=shop.a.id,
            new_variant_id=shop.b.id,
            confirmed_difference_amount=30,
            additional_payment_method='qr',
            request_key='test-higher-price-0002',
        ),
        shop.db,
        shop.cashier,
    )
    assert higher_res.difference_amount == 30
    assert higher_res.additional_payment_method == 'qr'



def test_month_end_refund_of_single_item_when_desired_item_does_not_exist(shop, monkeypatch):
    from api.routers.full_returns import preview, list_month_end_returns, settle_exchange_month_end
    from core.exchange_reporting import month_end_return_summary
    from db.models import Permission
    order = prepare(shop, monkeypatch)
    shop.cashier.role.permissions.append(Permission(name='sales:read')); shop.db.commit()
    month_end = dict(new_variant_id=shop.b.id, exchange_type='month_end_refund', reason='No existe la talla L')

    # Con stock disponible del ítem deseado se exige el cambio normal.
    with pytest.raises(HTTPException) as e:
        quote_exchange(order.id, GarmentExchangeQuoteRequest(original_variant_id=shop.a.id, branch_id=shop.branch.id, **month_end), shop.db, shop.cashier)
    assert e.value.status_code == 409

    shop.db.query(Inventory).filter_by(variant_id=shop.b.id, branch_id=shop.branch.id).update({'stock': 0}); shop.db.commit()
    quoted = quote_exchange(order.id, GarmentExchangeQuoteRequest(original_variant_id=shop.a.id, branch_id=shop.branch.id, **month_end), shop.db, shop.cashier)
    assert quoted['action'] == 'refund_month_end' and quoted['refund_amount'] == 120 and quoted['difference_amount'] == -120
    assert quoted['scheduled_for'].startswith('2026-10-31')

    row = exchange_garment(order.id, payload(shop, confirmed_difference_amount=-120, request_key='month-end-item-0001', **month_end), shop.db, shop.cashier)
    assert row.exchange_type == 'month_end_refund' and row.disbursement_status == 'PENDING_MONTH_END'
    assert row.additional_payment_method is None
    # La prenda recibida vuelve al stock y no se entrega ninguna otra.
    assert stock(shop) == 4 and stock(shop, shop.b.id) == 0

    # Solo se devuelve esa prenda: la otra unidad de la venta sigue vigente.
    assert preview(order.id, shop.db, shop.cashier, refund_timing='MONTH_END')['refund_amount'] == 120
    summary = month_end_return_summary(shop.db, shop.cashier)
    assert summary['pending_count'] == 1 and summary['pending_amount'] == 120
    listed = list_month_end_returns(shop.db, shop.cashier)
    assert listed[0]['source'] == 'exchange' and listed[0]['refund_amount'] == 120

    settled = settle_exchange_month_end(row.id, shop.db, shop.cashier)
    assert settled['disbursement_status'] == 'COMPLETED' and settled['settled_at'] is not None
    assert month_end_return_summary(shop.db, shop.cashier)['settled_amount'] == 120
