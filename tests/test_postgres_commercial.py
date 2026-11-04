"""Concurrent cash returns and supplier receipts in a disposable PostgreSQL schema."""
import os
from datetime import date
from types import SimpleNamespace
from uuid import uuid4
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from db.database import Base
from db.models import Tenant,Branch,Role,Permission,User,Product,ProductVariant,Inventory,Order,OrderItem
from db.models.full_return import FullReturn
from db.models.incoming import IncomingStock,IncomingReceipt
from db.models.operations import Supplier
from api.routers.full_returns import create as return_cash,ReturnInput
from api.routers.incoming import receive,ReceiptInput


@pytest.fixture
def pg():
    url=os.environ.get('PHASE6_POSTGRES_TEST_URL')
    if not url:pytest.skip('Temporary PostgreSQL not configured.')
    parsed=make_url(url)
    assert parsed.get_backend_name()=='postgresql' and (parsed.database or '').endswith('_test')
    schema='commercial_'+uuid4().hex
    admin=create_engine(url)
    with admin.begin() as connection:connection.execute(text(f'CREATE SCHEMA {schema}'))
    engine=create_engine(url,connect_args={'options':f'-csearch_path={schema} -clock_timeout=15000 -cstatement_timeout=30000'})
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            tenant=Tenant(name='Test',subdomain='test');db.add(tenant);db.flush()
            branch=Branch(tenant_id=tenant.id,name='Branch',address='Test')
            user=User(tenant_id=tenant.id,email='test@example.invalid',hashed_password='unused',branch=branch,
                role=Role(name='cajero',permissions=[Permission(name='sales:write'),Permission(name='inventory:write')]))
            product=Product(tenant_id=tenant.id,name='Shirt',base_price=100)
            variant=ProductVariant(tenant_id=tenant.id,product=product,sku='TEST',size='M',color='Blue')
            supplier=Supplier(tenant_id=tenant.id,name='Supplier')
            db.add_all([branch,user,product,variant,supplier]);db.flush()
            db.add(Inventory(tenant_id=tenant.id,branch_id=branch.id,variant_id=variant.id,stock=0))
            order=Order(tenant_id=tenant.id,user_id=user.id,branch_id=branch.id,total_amount=100,is_paid=True,payment_status='paid',
                payment_method='efectivo',payment_provider='Caja_Local',items=[OrderItem(variant_id=variant.id,quantity=1,unit_price=100)])
            incoming=IncomingStock(tenant_id=tenant.id,branch_id=branch.id,supplier_id=supplier.id,variant_id=variant.id,
                quantity=10,received_quantity=0,expected_date=date.today(),reference='PO',request_key='initial-incoming',request_fingerprint='fixture')
            db.add_all([order,incoming]);db.commit()
            values=SimpleNamespace(engine=engine,user_id=user.id,order_id=order.id,incoming_id=incoming.id)
        yield values
    finally:
        engine.dispose()
        with admin.begin() as connection:connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        admin.dispose()


@pytest.mark.parametrize('operation,same_key',[('return',True),('return',False),('receipt',True),('receipt',False)])
def test_simultaneous_requests_do_not_double_stock_or_money(pg,operation,same_key):
    barrier=Barrier(2)
    def worker(index):
        with Session(pg.engine) as db:
            user=db.get(User,pg.user_id)
            key='concurrent-operation-'+str(0 if same_key else index)
            barrier.wait(timeout=15)
            try:
                if operation=='return':
                    row=return_cash(pg.order_id,ReturnInput(request_key=key,confirmed_refund_amount=100,settlement_confirmed=True),db,user)
                    return 200,row['id']
                row=receive(pg.incoming_id,ReceiptInput(request_key=key,quantity=10),db,user)
                return 200,row['receipt_id']
            except HTTPException as error:return error.status_code,None
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures=[executor.submit(worker,i) for i in range(2)]
        results=[task.result(timeout=45) for task in futures]
    if same_key:assert results[0]==results[1] and results[0][0]==200
    else:assert sorted(code for code,_ in results)==[200,409]
    with Session(pg.engine) as db:
        assert db.query(Inventory).one().stock==(1 if operation=='return' else 10)
        assert db.query(FullReturn if operation=='return' else IncomingReceipt).count()==1
