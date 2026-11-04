"""Opt-in concurrency evidence using a temporary schema in a disposable *_test DB."""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from db.database import Base
from db.models import Tenant,SubscriptionPlan,Subscription,Branch,Role,Permission,User,Product,ProductVariant,Inventory,Order,SyncOperation
from core.offline_sync import process_batch
from schemas.sync import SyncBatchRequest


@pytest.mark.postgres
def test_concurrent_identical_uuid_with_exact_stock():
    url=os.environ.get('PHASE6_POSTGRES_TEST_URL')
    if not url:pytest.skip('PHASE6_POSTGRES_TEST_URL no configurada; no se afirma validación PostgreSQL.')
    parsed=make_url(url)
    if parsed.get_backend_name()!='postgresql' or not (parsed.database or '').endswith('_test'):
        pytest.fail('Use únicamente una base PostgreSQL desechable cuyo nombre termine en _test.')
    schema='phase6_'+uuid4().hex
    admin=create_engine(url)
    with admin.begin() as conn:conn.execute(text(f'CREATE SCHEMA {schema}'))
    engine=create_engine(url,connect_args={'options':f'-csearch_path={schema}'},pool_pre_ping=True)
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            tenant=Tenant(name='Concurrent test',subdomain='concurrent')
            plan=SubscriptionPlan(name='Test',code='test',has_offline_sync=True)
            db.add_all([tenant,plan]);db.flush()
            db.add(Subscription(tenant_id=tenant.id,plan_id=plan.id,status='active'))
            branch=Branch(tenant_id=tenant.id,name='Test',address='Test')
            user=User(tenant_id=tenant.id,branch=branch,email='test@example.invalid',hashed_password='unused',role=Role(name='cajero',permissions=[Permission(name='sales:write')]))
            product=Product(tenant_id=tenant.id,name='Shirt',base_price=120)
            variant=ProductVariant(tenant_id=tenant.id,product=product,sku='TEST',size='M',color='Blue')
            db.add_all([branch,user,product,variant]);db.flush()
            db.add(Inventory(tenant_id=tenant.id,branch_id=branch.id,variant_id=variant.id,stock=2));db.commit()
            user_id,branch_id,variant_id=user.id,branch.id,variant.id
        barrier=Barrier(2)
        def worker():
            with Session(engine) as db:
                actor=db.get(User,user_id)
                batch=SyncBatchRequest(branch_id=branch_id,sales=[dict(client_operation_uuid='phase-six-concurrent',user_id=user_id,payment_method='efectivo',total_amount=240,items=[dict(variant_id=variant_id,quantity=2,unit_price=120)])])
                barrier.wait(timeout=15)
                return process_batch(batch,db,actor).model_dump(mode='json')
        with ThreadPoolExecutor(max_workers=2) as executor:
            tasks=[executor.submit(worker) for _ in range(2)]
            first,second=[task.result(timeout=45) for task in tasks]
        assert first==second and first['confirmed_count']==1
        with Session(engine) as db:
            assert db.query(Inventory).one().stock==0
            assert db.query(Order).count()==db.query(SyncOperation).count()==1
    finally:
        engine.dispose()
        with admin.begin() as conn:conn.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        admin.dispose()
