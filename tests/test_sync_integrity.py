"""Regression tests use a disposable database, never SessionLocal or demo accounts."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from db.database import Base
from db.models import (Tenant, Subscription, SubscriptionPlan, Branch, User, Role,
    Permission, UserPermission, Product, ProductVariant, Inventory, Order, Reservation)
from db.models.sync_operation import SyncOperation
from core.offline_sync import process_batch
from schemas.sync import SyncBatchRequest


@pytest.fixture
def shop():
    engine = create_engine('sqlite:///:memory:')
    @event.listens_for(engine, 'connect')
    def configure(conn, _):
        conn.isolation_level = None
        conn.execute('PRAGMA foreign_keys=ON')
    @event.listens_for(engine, 'begin')
    def begin(conn):
        conn.exec_driver_sql('BEGIN')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        tenant = Tenant(name='Test', subdomain='test')
        other = Tenant(name='Other', subdomain='other')
        plan = SubscriptionPlan(name='Test', code='test', has_offline_sync=True)
        db.add_all([tenant, other, plan]); db.flush()
        db.add(Subscription(tenant_id=tenant.id, plan_id=plan.id, status='active'))
        branch = Branch(name='A', address='A', tenant_id=tenant.id)
        role = Role(name='administrador', permissions=[Permission(name='sales:write'), Permission(name='reservations:write')])
        user = User(email='staff@example.invalid', hashed_password='unused', role=role, branch=branch, tenant_id=tenant.id)
        customer = User(email='customer@example.invalid', hashed_password='unused', tenant_id=tenant.id, role=Role(name='cliente'))
        foreign = User(email='foreign@example.invalid', hashed_password='unused', tenant_id=other.id)
        product = Product(name='Camisa', base_price=120, tenant_id=tenant.id)
        variant = ProductVariant(product=product, tenant_id=tenant.id, size='M', color='Azul', sku='TEST')
        db.add_all([branch,user,customer,foreign,product,variant]); db.flush()
        db.add(Inventory(branch_id=branch.id, variant_id=variant.id, stock=5, tenant_id=tenant.id)); db.commit()
        yield SimpleNamespace(db=db, tenant=tenant, user=user, customer=customer, foreign=foreign, branch=branch, variant=variant)
    engine.dispose()


def sale(s, uid='operation-test-0001', qty=2, price=120, **extra):
    return dict(client_operation_uuid=uid, user_id=s.customer.id, payment_method='efectivo',
        total_amount=qty*price, items=[dict(variant_id=s.variant.id,quantity=qty,unit_price=price)], **extra)


def send(s, sales=None, reservations=None, user=None):
    return process_batch(SyncBatchRequest(branch_id=s.branch.id, sales=sales or [], reservations=reservations or []), s.db, user or s.user)


def stock(s):
    s.db.expire_all()
    return s.db.query(Inventory).one().stock


def test_success_repeat_is_exact_receipt(shop):
    first = send(shop, [sale(shop)])
    again = send(shop, [sale(shop)])
    assert first == again
    assert stock(shop) == 3
    assert shop.db.query(Order).count() == 1


def test_conflict_is_stable_even_after_restock(shop):
    first = send(shop, [sale(shop, qty=10)])
    assert first.receipts[0].status == 'RECHAZADA_CONFLICTO'
    shop.db.query(Inventory).update({'stock': 20}); shop.db.commit()
    assert send(shop, [sale(shop, qty=10)]) == first
    assert stock(shop) == 20
    assert shop.db.query(Order).count() == 0


@pytest.mark.parametrize('change', ['quantity', 'type'])
def test_uuid_changed_payload_rejected(shop, change):
    send(shop, [sale(shop)])
    with pytest.raises(HTTPException) as error:
        if change == 'quantity': send(shop, [sale(shop, qty=1)])
        else: send(shop, reservations=[dict(client_operation_uuid='operation-test-0001',user_id=shop.customer.id,items=[dict(variant_id=shop.variant.id,quantity=2)])])
    assert error.value.status_code == 409
    assert stock(shop) == 3


def test_price_mismatch_does_not_confirm_or_debit(shop):
    result = send(shop, [sale(shop, price=100)])
    assert result.conflict_count == 1
    assert stock(shop) == 5
    assert shop.db.query(Order).count() == 0


def test_business_conflict_does_not_rollback_valid_operation(shop):
    result = send(shop, [sale(shop), sale(shop, uid='operation-test-0002', qty=20)])
    assert result.confirmed_count == result.conflict_count == 1
    assert stock(shop) == 3
    assert shop.db.query(SyncOperation).count() == 2


def test_unexpected_failure_rolls_back_batch_and_receipts(shop):
    def fail(session, *_):
        if any(isinstance(o, SyncOperation) and o.client_operation_uuid=='operation-test-0002' for o in session.new):
            raise RuntimeError('injected persistence failure')
    event.listen(shop.db, 'before_flush', fail)
    with pytest.raises(RuntimeError):
        send(shop,[sale(shop),sale(shop,uid='operation-test-0002',qty=20)])
    event.remove(shop.db,'before_flush',fail)
    assert stock(shop)==5
    assert shop.db.query(Order).count()==shop.db.query(SyncOperation).count()==0


def test_revoked_admin_permission_is_respected(shop):
    perm=shop.db.query(Permission).filter_by(name='sales:write').one()
    shop.db.add(UserPermission(user_id=shop.user.id,permission_id=perm.id,is_granted=False));shop.db.commit()
    with pytest.raises(HTTPException) as error: send(shop,[sale(shop)])
    assert error.value.status_code==403
    assert stock(shop)==5


def test_foreign_customer_rejected(shop):
    op=sale(shop);op['user_id']=shop.foreign.id
    assert send(shop,[op]).conflict_count==1
    assert stock(shop)==5


def test_consumer_own_reservation_only(shop):
    op=dict(client_operation_uuid='reservation-test-01',user_id=shop.customer.id,items=[dict(variant_id=shop.variant.id,quantity=1)])
    assert send(shop,reservations=[op],user=shop.customer).confirmed_count==1
    op['user_id']=shop.user.id
    with pytest.raises(HTTPException): send(shop,reservations=[op],user=shop.customer)
    assert stock(shop)==4
    assert shop.db.query(Reservation).count()==1


@pytest.mark.parametrize('case',['inactive','expired','no_tenant','no_branch'])
def test_invalid_context_rejected(shop,case):
    if case=='inactive':shop.tenant.is_active=False
    elif case=='expired':shop.tenant.active_subscription.end_date=datetime.now(timezone.utc)-timedelta(days=1)
    elif case=='no_tenant':shop.user.tenant_id=None
    else:shop.user.branch_id=None
    shop.db.commit()
    with pytest.raises(HTTPException) as error: send(shop,[sale(shop)])
    assert error.value.status_code==403
    assert stock(shop)==5
