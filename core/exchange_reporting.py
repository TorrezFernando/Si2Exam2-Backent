from fastapi import HTTPException
from sqlalchemy import func
from db.models.sale import GarmentExchange, Order
from core.access import company_id
from core.commerce import money
from api.deps import get_user_permissions


def exchange_summary(db,user,start=None,end=None,order_type=None,payment_method=None):
    if 'sales:read' not in get_user_permissions(db,user):
        raise HTTPException(403,'No tienes permisos para ver reportes.')
    tenant=company_id(user)
    query=db.query(GarmentExchange).join(Order,Order.id==GarmentExchange.order_id).filter(
        GarmentExchange.tenant_id==tenant,Order.tenant_id==tenant,GarmentExchange.status=='APPROVED')
    if 'branches:write' not in get_user_permissions(db,user):
        if not user.branch_id:
            raise HTTPException(403,'No tienes sucursal asignada.')
        query=query.filter(GarmentExchange.branch_id==user.branch_id)
    if start: query=query.filter(func.date(GarmentExchange.created_at)>=start)
    if end: query=query.filter(func.date(GarmentExchange.created_at)<=end)
    if order_type: query=query.filter(Order.order_type==order_type)
    # The settlement itself is cash, even if the original purchase was digital.
    if payment_method and payment_method!='efectivo':
        return dict(collected=0,refunded=0,net=0,unverified_count=0,count=0)
    collected=money(0);refunded=money(0);unverified=0
    rows=query.all()
    for row in rows:
        if row.settlement_method is None:
            unverified+=1
        elif row.settlement_method=='cash':
            amount=money(row.difference_amount)
            if amount>0: collected+=amount
            else: refunded-=amount
    return dict(collected=float(collected),refunded=float(refunded),net=float(collected-refunded),unverified_count=unverified,count=len(rows))


def full_return_summary(db,user,start=None,end=None,order_type=None,payment_method=None):
    from db.models.full_return import FullReturn
    if 'sales:read' not in get_user_permissions(db,user):
        raise HTTPException(403,'No tienes permisos para ver reportes.')
    tenant=company_id(user)
    query=db.query(FullReturn).join(Order,Order.id==FullReturn.order_id).filter(FullReturn.tenant_id==tenant,Order.tenant_id==tenant)
    if 'branches:write' not in get_user_permissions(db,user):
        if not user.branch_id: raise HTTPException(403,'No tienes sucursal asignada.')
        query=query.filter(FullReturn.branch_id==user.branch_id)
    if start: query=query.filter(func.date(FullReturn.created_at)>=start)
    if end: query=query.filter(func.date(FullReturn.created_at)<=end)
    if order_type: query=query.filter(Order.order_type==order_type)
    if payment_method and payment_method!='efectivo':return dict(count=0,refunded=0)
    rows=query.all()
    return dict(count=len(rows),refunded=float(sum((money(r.refund_amount) for r in rows),money(0))))


def month_end_return_summary(db,user,start=None,end=None,order_type=None):
    from db.models.full_return import FullReturn
    if 'sales:read' not in get_user_permissions(db,user):
        raise HTTPException(403,'No tienes permisos para ver reportes.')
    tenant=company_id(user)
    query=db.query(FullReturn).join(Order,Order.id==FullReturn.order_id).filter(
        FullReturn.tenant_id==tenant,
        Order.tenant_id==tenant,
        FullReturn.refund_timing=='MONTH_END',
    )
    if 'branches:write' not in get_user_permissions(db,user):
        if not user.branch_id: raise HTTPException(403,'No tienes sucursal asignada.')
        query=query.filter(FullReturn.branch_id==user.branch_id)
    if start: query=query.filter(func.date(FullReturn.created_at)>=start)
    if end: query=query.filter(func.date(FullReturn.created_at)<=end)
    if order_type: query=query.filter(Order.order_type==order_type)
    rows=[(r.disbursement_status,money(r.refund_amount)) for r in query.all()]
    # Devoluciones por prenda registradas desde Cambios cuando el ítem deseado no existía.
    exchanges=db.query(GarmentExchange).join(Order,Order.id==GarmentExchange.order_id).filter(
        GarmentExchange.tenant_id==tenant,Order.tenant_id==tenant,
        GarmentExchange.status=='APPROVED',GarmentExchange.settlement_method=='month_end')
    if 'branches:write' not in get_user_permissions(db,user):
        exchanges=exchanges.filter(GarmentExchange.branch_id==user.branch_id)
    if start: exchanges=exchanges.filter(func.date(GarmentExchange.created_at)>=start)
    if end: exchanges=exchanges.filter(func.date(GarmentExchange.created_at)<=end)
    if order_type: exchanges=exchanges.filter(Order.order_type==order_type)
    rows+=[(e.disbursement_status,-money(e.difference_amount)) for e in exchanges.all()]
    pending=[amount for status,amount in rows if status=='PENDING_MONTH_END']
    settled=[amount for status,amount in rows if status=='COMPLETED']
    return dict(
        count=len(rows),
        pending_count=len(pending),
        pending_amount=float(sum(pending,money(0))),
        settled_count=len(settled),
        settled_amount=float(sum(settled,money(0))),
    )
