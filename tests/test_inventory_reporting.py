from datetime import datetime, timedelta, timezone
import pytest
from fastapi import HTTPException
from test_sync_integrity import shop
from db.models import Permission, Reservation, ReservationItem, Order, OrderItem, ProductVariant, GarmentExchange, Branch
from db.models.incoming import IncomingStock
from db.models.operations import Supplier
from api.routers.inventory_reports import snapshot, export
from core.exchange_reporting import exchange_summary


def setup(shop):
    shop.user.role.permissions.extend([Permission(name='inventory:read'),Permission(name='sales:read')])
    shop.db.commit()


def test_snapshot_reserved_pending_sold_exchanges_incoming_without_double_count(shop):
    setup(shop)
    v2=ProductVariant(product_id=shop.variant.product_id,tenant_id=shop.tenant.id,size='L',color='Blue',sku='SECOND')
    supplier=Supplier(name='Supplier',tenant_id=shop.tenant.id)
    shop.db.add_all([v2,supplier]);shop.db.flush()
    reservation=Reservation(tenant_id=shop.tenant.id,user_id=shop.customer.id,branch_id=shop.branch.id,status='pending',items=[ReservationItem(variant_id=shop.variant.id,quantity=2)])
    cancelled=Reservation(tenant_id=shop.tenant.id,user_id=shop.customer.id,branch_id=shop.branch.id,status='cancelled',items=[ReservationItem(variant_id=shop.variant.id,quantity=99)])
    paid=Order(tenant_id=shop.tenant.id,user_id=shop.customer.id,branch_id=shop.branch.id,total_amount=360,is_paid=True,payment_status='paid',items=[OrderItem(variant_id=shop.variant.id,quantity=3,unit_price=120)])
    pending=Order(tenant_id=shop.tenant.id,user_id=shop.customer.id,branch_id=shop.branch.id,total_amount=120,is_paid=False,payment_status='pending',items=[OrderItem(variant_id=shop.variant.id,quantity=1,unit_price=120)])
    shop.db.add_all([reservation,cancelled,paid,pending]);shop.db.flush()
    shop.db.add(GarmentExchange(tenant_id=shop.tenant.id,order_id=paid.id,branch_id=shop.branch.id,user_id=shop.user.id,
        original_variant_id=shop.variant.id,new_variant_id=v2.id,quantity=1,status='APPROVED',difference_amount=10,settlement_method='cash'))
    shop.db.add(IncomingStock(tenant_id=shop.tenant.id,branch_id=shop.branch.id,supplier_id=supplier.id,variant_id=v2.id,quantity=10,received_quantity=4,
        expected_date=datetime.now(timezone.utc).date(),reference='PO',status='partial',request_key='incoming-report-test',request_fingerprint='test'))
    shop.db.commit()
    result=snapshot(shop.db,shop.user)
    assert result['count']==2
    assert result['totals']==dict(available=5,reserved=2,payment_pending=1,physical=8,sold_net=3,incoming=6)
    rows={r['variant_id']:r for r in result['data']}
    assert rows[shop.variant.id]['sold_net']==2
    assert rows[v2.id]['sold_net']==1
    assert rows[v2.id]['available']==0
    assert exchange_summary(shop.db,shop.user)['net']==10


def test_report_branch_access_does_not_leak_foreign_data(shop):
    setup(shop)
    foreign=Branch(name='Foreign',address='Private',tenant_id=shop.foreign.tenant_id)
    shop.db.add(foreign);shop.db.commit()
    with pytest.raises(HTTPException) as error:
        snapshot(shop.db,shop.user,foreign.id)
    assert error.value.status_code==403
    shop.user.role.permissions.append(Permission(name='branches:write'));shop.db.commit()
    with pytest.raises(HTTPException) as error:
        snapshot(shop.db,shop.user,foreign.id)
    assert error.value.status_code==404
    assert {r['branch_id'] for r in snapshot(shop.db,shop.user)['data']}=={shop.branch.id}


def test_exchange_report_uses_event_date_and_cash_method(shop):
    setup(shop)
    order=Order(tenant_id=shop.tenant.id,user_id=shop.customer.id,branch_id=shop.branch.id,total_amount=120,is_paid=True,payment_status='paid',payment_method='tarjeta',created_at=datetime(2026,1,1))
    shop.db.add(order);shop.db.flush()
    for amount,method in [(20,'cash'),(-5,'cash'),(0,'none'),(100,None)]:
        shop.db.add(GarmentExchange(tenant_id=shop.tenant.id,order_id=order.id,branch_id=shop.branch.id,user_id=shop.user.id,
            original_variant_id=shop.variant.id,new_variant_id=shop.variant.id,quantity=1,status='APPROVED',difference_amount=amount,
            settlement_method=method,created_at=datetime(2026,10,31)))
    shop.db.commit()
    day=datetime(2026,10,31).date()
    totals=exchange_summary(shop.db,shop.user,day,day,payment_method='efectivo')
    assert totals==dict(collected=20,refunded=5,net=15,unverified_count=1,count=4)
    assert exchange_summary(shop.db,shop.user,day,day,payment_method='tarjeta')['net']==0
    assert exchange_summary(shop.db,shop.user,end=day-timedelta(days=1))['count']==0


def test_report_endpoints_exports_permissions_and_csv_escaping(shop):
    import io
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api import deps
    from api.routers import reports
    from db.database import get_db
    from openpyxl import load_workbook
    setup(shop)
    shop.variant.product.name='=SUM(1,2)'
    shop.db.commit()
    app=FastAPI();app.include_router(reports.router,prefix='/reports')
    app.dependency_overrides[deps.get_db]=lambda:shop.db
    app.dependency_overrides[get_db]=lambda:shop.db
    app.dependency_overrides[deps.get_current_active_user]=lambda:shop.user
    api=TestClient(app)
    response=api.get('/reports/inventory')
    assert response.status_code==200,response.text
    assert response.json()['totals']['available']==5
    csv=api.get('/reports/inventory/export')
    assert csv.status_code==200
    assert "'=SUM(1,2)" in csv.text
    assert api.get('/reports/sales?start_date=2026-10-31&end_date=2026-01-01').status_code==422
    pdf=api.get('/reports/sales/export?format=pdf')
    assert pdf.status_code==200,pdf.text
    assert pdf.content.startswith(b'%PDF')
    excel=api.get('/reports/sales/export?format=excel')
    assert excel.status_code==200
    values=list(load_workbook(io.BytesIO(excel.content)).active.values)
    assert any(row[0]=='Cobrado' for row in values)
    app.dependency_overrides[deps.get_current_active_user]=lambda:shop.customer
    assert api.get('/reports/inventory').status_code==403
    assert api.get('/reports/inventory/export').status_code==403
