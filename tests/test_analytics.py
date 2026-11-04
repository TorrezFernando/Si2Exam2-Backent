from datetime import date, timedelta, datetime, time
from types import SimpleNamespace
import asyncio
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from test_sync_integrity import shop,send,sale
from db.models import Permission,Order,OrderItem
from schemas.analytics import ReportSpec
from core.analytics import report


@pytest.fixture
def analytics(shop):
    shop.tenant.active_subscription.plan.has_ai_reports=True
    shop.user.role.permissions.append(Permission(name='sales:read'))
    shop.db.commit()
    send(shop,[sale(shop)])
    # SQLite's default timestamp is UTC; date.today() is local. Keep the fixture
    # inside the report date even during the UTC/local midnight boundary.
    shop.db.query(Order).update({'created_at': datetime.combine(date.today(), time(12))})
    shop.db.commit()
    return shop


def spec(kind='monthly_branch',**extra):
    return ReportSpec(kind=kind,start=date.today()-timedelta(days=30),end=date.today(),**extra)


@pytest.mark.parametrize('kind',['monthly_branch','top_products','weekly_sales','quarterly_branches','low_stock','category_revenue'])
def test_six_reports(analytics,kind):
    data=report(analytics.db,analytics.user,SimpleNamespace(headers={}),spec(kind))
    assert len(data['rows'])==1
    assert data['rows'][0][1]==(2 if kind=='top_products' else 3 if kind=='low_stock' else 240)


def test_plan_and_filters_cannot_escape_tenant(analytics):
    with pytest.raises(HTTPException) as e:report(analytics.db,analytics.user,SimpleNamespace(headers={}),spec(branch_id=9999))
    assert e.value.status_code==403
    analytics.tenant.active_subscription.plan.has_ai_reports=False;analytics.db.commit()
    with pytest.raises(HTTPException) as e:report(analytics.db,analytics.user,SimpleNamespace(headers={}),spec())
    assert e.value.status_code==403


def test_untrusted_schema_and_dates():
    with pytest.raises(ValidationError):ReportSpec(**{**spec().model_dump(),'tenant_id':2})
    with pytest.raises(ValidationError):ReportSpec(kind='raw_sql',start=date.today(),end=date.today())
    with pytest.raises(ValidationError):ReportSpec(kind='top_products',start=date.today(),end=date.today()-timedelta(days=1))


def test_foreign_sales_do_not_enter_report(analytics):
    from db.models import Branch,Product,ProductVariant
    db=analytics.db;tid=analytics.foreign.tenant_id
    branch=Branch(tenant_id=tid,name='Foreign',address='x')
    product=Product(tenant_id=tid,name='Foreign',base_price=999)
    variant=ProductVariant(tenant_id=tid,product=product,sku='FOREIGN',size='M',color='X')
    db.add_all([branch,product,variant]);db.flush()
    order=Order(tenant_id=tid,branch_id=branch.id,user_id=analytics.foreign.id,is_paid=True,payment_status='paid',total_amount=999)
    db.add(order);db.flush();db.add(OrderItem(tenant_id=tid,order_id=order.id,variant_id=variant.id,quantity=1,unit_price=999));db.commit()
    result=report(db,analytics.user,SimpleNamespace(headers={}),spec())
    assert sum(row[1] for row in result['rows'])==240


def test_interpreter_without_key_is_explicit(analytics,monkeypatch):
    from api.routers.analytics import interpret
    from schemas.analytics import PromptIn
    monkeypatch.setattr('core.ai_service._get_api_keys',lambda:[])
    with pytest.raises(HTTPException) as e:asyncio.run(interpret(PromptIn(prompt='ventas de este mes'),SimpleNamespace(headers={}),analytics.db,analytics.user))
    assert e.value.status_code==503


def test_exports(analytics):
    from api.routers.analytics import export
    async def collect(response):return b''.join([chunk async for chunk in response.body_iterator])
    for fmt,magic in [('pdf',b'%PDF'),('xlsx',b'PK')]:
        response=export(fmt,spec(),SimpleNamespace(headers={}),analytics.db,analytics.user)
        assert asyncio.run(collect(response)).startswith(magic)


def test_ml_holdout_and_demo_are_labelled(tmp_path,monkeypatch):
    from ml_engine import engine
    monkeypatch.setattr(engine,'__file__',str(tmp_path/'engine.py'))
    result=engine.analyze(engine.synthetic(),2,demo=True)
    assert result['source']=='synthetic'
    assert len(result['forecast'])==6
    assert result['metrics']['train_rows']>0 and result['metrics']['test_rows']>0
    assert -1<=result['silhouette']<=1
    assert list((tmp_path/'artifacts/tenant_2').glob('*.joblib'))
    with pytest.raises(ValueError):engine.analyze([],2)


def test_ml_accepts_eight_complete_months_and_rejects_seven(tmp_path, monkeypatch):
    from ml_engine import engine
    import pandas as pd
    monkeypatch.setattr(engine, '__file__', str(tmp_path / 'engine.py'))
    rows = engine.synthetic()
    months = sorted({row['date'][:7] for row in rows})
    current_month = pd.Period(date.today(), freq='M')
    assert months == [str(current_month - offset) for offset in range(8, 0, -1)]
    result = engine.analyze(rows, 2, demo=True)
    assert result['metrics']['train_rows'] == 36
    assert result['metrics']['test_rows'] == 12
    assert {item['month'] for item in result['forecast']} == {str(current_month)}
    with pytest.raises(ValueError, match='8 meses'):
        engine.analyze([row for row in rows if row['date'][:7] != months[0]], 2, demo=True)
