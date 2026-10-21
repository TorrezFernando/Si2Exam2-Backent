from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from db.database import Base
from db.models import User, Role, Permission, Branch, Product, ProductVariant, Inventory, Order, OrderItem, Reservation, InventoryMovement
from db.models.rbac import UserPermission
from db.models.sale import OrderTypeEnum, PaymentStatusEnum
from schemas.sale import OrderCreate
from schemas.reservation import ReservationCreate, ReservationUpdate
from api.routers.orders import create_order, orders_query, cancel_order
from api.routers.reservations import create_reservation, cancel_reservation, update_reservation
from api.routers.products import delete_product
from api.routers.payments import reconcile, callback


@pytest.fixture
def shop():
    engine = create_engine('sqlite:///:memory:')
    @event.listens_for(engine, 'connect')
    def foreign_keys(conn, _):
        conn.execute('PRAGMA foreign_keys=ON')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        branch = Branch(name='A', address='A'); other = Branch(name='B', address='B')
        role = Role(name='cajero', permissions=[Permission(name='sales:write'), Permission(name='reservations:write')])
        customer = User(email='customer@example.com', hashed_password='unused', role=Role(name='cliente'))
        cashier = User(email='cashier@example.com', hashed_password='unused', role=role, branch=branch)
        product = Product(name='Camisa', base_price=120)
        a = ProductVariant(product=product, size='M', color='Azul', sku='A')
        b = ProductVariant(product=product, size='L', color='Azul', sku='B', price_override=150)
        db.add_all([branch, other, customer, cashier, product, a, b]); db.flush()
        db.add_all([Inventory(branch_id=branch.id, variant_id=a.id, stock=5),
                    Inventory(branch_id=branch.id, variant_id=b.id, stock=1),
                    Inventory(branch_id=other.id, variant_id=a.id, stock=20)])
        db.commit()
        yield SimpleNamespace(db=db, branch=branch, other=other, customer=customer, cashier=cashier, product=product, a=a, b=b)


def stock(s, variant=None, branch=None):
    s.db.expire_all()
    return s.db.query(Inventory).filter_by(branch_id=branch or s.branch.id, variant_id=variant or s.a.id).one().stock


def reserve(s, items=None):
    return create_reservation(db=s.db, current_user=s.customer, reservation_in=ReservationCreate(
        branch_id=s.branch.id, pickup_date=datetime.now(timezone.utc)+timedelta(days=1),
        items=items or [{'variant_id': s.a.id, 'quantity': 2}]))


def sale(s, items=None, **extra):
    return create_order(db=s.db, current_user=s.cashier, order_in=OrderCreate(branch_id=s.branch.id,
        items=items or [{'variant_id': s.a.id, 'quantity': 2, 'unit_price': 0.01}], **extra))


@pytest.mark.parametrize('quantity', [0, -1])
def test_reject_invalid_quantity(quantity):
    with pytest.raises(ValidationError):
        OrderCreate(items=[{'variant_id': 1, 'quantity': quantity}])
    with pytest.raises(ValidationError):
        ReservationCreate(branch_id=1, items=[{'variant_id': 1, 'quantity': quantity}])


def test_reject_empty_cart_and_past_pickup():
    with pytest.raises(ValidationError):
        OrderCreate(items=[])
    with pytest.raises(ValidationError):
        ReservationCreate(branch_id=1, items=[{'variant_id':1, 'quantity':1}], pickup_date=datetime.now(timezone.utc)-timedelta(days=1))


def test_order_uses_catalog_price_and_own_branch(shop):
    order = sale(shop)
    assert order.total_amount == 240
    assert order.is_paid
    assert stock(shop) == 3
    assert stock(shop, branch=shop.other.id) == 20
    assert shop.db.query(InventoryMovement).count() == 1


def test_sale_rolls_back_every_item(shop):
    with pytest.raises(HTTPException) as error:
        sale(shop, [{'variant_id':shop.a.id, 'quantity':2}, {'variant_id':shop.b.id, 'quantity':2}])
    assert error.value.status_code == 409
    assert stock(shop) == 5
    assert shop.db.query(Order).count() == 0
    assert shop.db.query(OrderItem).count() == 0
    assert shop.db.query(InventoryMovement).count() == 0


def test_reservation_failure_restores_previous_items(shop):
    with pytest.raises(HTTPException):
        reserve(shop, [{'variant_id':shop.a.id, 'quantity':2}, {'variant_id':shop.b.id, 'quantity':2}])
    assert stock(shop) == 5
    assert shop.db.query(Reservation).count() == 0
    assert shop.db.query(InventoryMovement).count() == 0


def test_reservation_cancel_is_idempotent_and_uses_base_price(shop):
    reservation = reserve(shop)
    assert reservation.items[0].unit_price == 120
    assert stock(shop) == 3
    cancel_reservation(reservation.id, shop.db, shop.customer)
    cancel_reservation(reservation.id, shop.db, shop.customer)
    assert stock(shop) == 5


def test_reservation_does_not_borrow_stock_from_other_branch(shop):
    with pytest.raises(HTTPException):
        create_reservation(db=shop.db, current_user=shop.customer, reservation_in=ReservationCreate(
            branch_id=shop.other.id, items=[{'variant_id':shop.b.id, 'quantity':1}]))
    assert stock(shop, variant=shop.b.id) == 1


def test_reservation_partial_purchase_releases_unselected_prendas(shop):
    reservation = reserve(shop, [{'variant_id':shop.a.id, 'quantity':3}, {'variant_id':shop.b.id, 'quantity':1}])
    order = sale(shop, [{'variant_id':shop.a.id, 'quantity':1}], order_type='reserva_pickup', reservation_id=reservation.id)
    assert order.user_id == shop.customer.id
    assert stock(shop) == 4
    assert stock(shop, variant=shop.b.id) == 1
    assert reservation.status.value == 'completed'
    with pytest.raises(HTTPException):
        cancel_reservation(reservation.id, shop.db, shop.customer)


def test_reservation_cannot_be_completed_without_sale(shop):
    reservation = reserve(shop)
    with pytest.raises(HTTPException):
        update_reservation(reservation.id, ReservationUpdate(status='completed'), shop.db, shop.cashier)
    assert stock(shop) == 3


def test_cashier_cannot_write_other_branch(shop):
    with pytest.raises(HTTPException) as error:
        create_order(db=shop.db, current_user=shop.cashier, order_in=OrderCreate(branch_id=shop.other.id,
            items=[{'variant_id':shop.a.id,'quantity':1}]))
    assert error.value.status_code == 403


def test_customer_cannot_fake_pos_payment(shop):
    with pytest.raises(HTTPException) as error:
        create_order(db=shop.db, current_user=shop.customer, order_in=OrderCreate(branch_id=shop.branch.id,
            items=[{'variant_id':shop.a.id,'quantity':1}]))
    assert error.value.status_code == 403


def test_repeated_checkout_does_not_duplicate_stock_discount(shop):
    a = sale(shop, request_key='stable-request-key')
    b = sale(shop, request_key='stable-request-key')
    assert a.id == b.id
    assert stock(shop) == 3


def test_unknown_role_only_sees_own_orders(shop):
    sale(shop)
    user = User(email='custom@example.com', hashed_password='unused', role=Role(name='custom'))
    shop.db.add(user); shop.db.commit()
    assert orders_query(shop.db, user).count() == 0


def test_revoked_sale_permission_is_respected(shop):
    perm = shop.db.query(Permission).filter_by(name='sales:write').one()
    shop.db.add(UserPermission(user_id=shop.cashier.id, permission_id=perm.id, is_granted=False)); shop.db.commit()
    with pytest.raises(HTTPException):
        sale(shop)


def test_product_deactivation_keeps_sale_history(shop):
    order = sale(shop)
    delete_product(db=shop.db, product_id=shop.product.id, current_user=shop.cashier)
    assert not shop.product.is_active
    assert shop.db.get(Order, order.id).items[0].variant.product.name == 'Camisa'
    with pytest.raises(HTTPException):
        sale(shop)


def test_online_payment_never_automatically_paid(shop, monkeypatch):
    from core import libelula
    monkeypatch.setattr(libelula, 'ensure_configured', lambda: None)
    order = create_order(db=shop.db, current_user=shop.customer, order_in=OrderCreate(
        items=[{'variant_id':shop.a.id,'quantity':1}]))
    assert not order.is_paid and order.payment_status == PaymentStatusEnum.pending
    order.payment_id = 'provider-transaction'; shop.db.commit()
    monkeypatch.setattr(libelula, 'is_paid', lambda _: False)
    with pytest.raises(HTTPException):
        callback(order.payment_id, shop.db)
    assert not order.is_paid
    monkeypatch.setattr(libelula, 'is_paid', lambda _: True)
    reconcile(order, shop.db); reconcile(order, shop.db)
    assert order.is_paid
    assert stock(shop) == 4


def test_payment_amount_must_match_provider_response(monkeypatch):
    from core import libelula
    order = SimpleNamespace(created_at=datetime.now(timezone.utc), payment_id='id', transaction_ref='ref', total_amount=120)
    payment = {'id_transaccion':'id', 'identificador':'ref', 'fecha_pago':'2026-10-15', 'moneda':'BOB','monto_pagado':1}
    monkeypatch.setattr(libelula, 'call', lambda *_: {'datos':[payment]})
    assert not libelula.is_paid(order)
    payment['monto_pagado'] = 120
    assert libelula.is_paid(order)


def test_unpaid_online_order_cancellation_releases_held_stock(shop, monkeypatch):
    from core import libelula
    monkeypatch.setattr(libelula, 'ensure_configured', lambda: None)
    order = create_order(db=shop.db, current_user=shop.customer, order_in=OrderCreate(
        items=[{'variant_id': shop.a.id, 'quantity': 2}]))
    assert stock(shop) == 3
    assert order.payment_status == PaymentStatusEnum.pending
    cancelled = cancel_order(order.id, shop.db, shop.customer)
    assert cancelled.payment_status == PaymentStatusEnum.failed
    assert stock(shop) == 5


def test_reservation_pickup_honors_reserved_price_even_if_base_price_changes(shop):
    reservation = reserve(shop, [{'variant_id': shop.a.id, 'quantity': 1}])
    assert reservation.items[0].unit_price == 120
    shop.product.base_price = 250
    shop.db.commit()
    order = sale(shop, [{'variant_id': shop.a.id, 'quantity': 1}], order_type='reserva_pickup', reservation_id=reservation.id)
    assert order.total_amount == 120
    assert order.items[0].unit_price == 120

