"""Conventional stock snapshot, independent of the AI subscription feature."""
import csv
import io
from collections import defaultdict
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session
from api.deps import get_db, require_permission, get_user_permissions
from core.access import company_id
from db.models import Branch, Inventory, Product, ProductVariant, Order, OrderItem, Reservation, ReservationItem, GarmentExchange
from db.models.incoming import IncomingStock
from db.models.reservation import ReservationStatus
from db.models.sale import PaymentStatusEnum

router = APIRouter()


def snapshot(db, user, branch_id=None):
    tenant = company_id(user)
    branches = db.query(Branch).filter(Branch.tenant_id == tenant)
    if 'branches:write' not in get_user_permissions(db, user):
        if not user.branch_id or branch_id not in (None, user.branch_id):
            raise HTTPException(403, 'Solo puedes consultar tu sucursal asignada.')
        branch_id = user.branch_id
    if branch_id is not None:
        branches = branches.filter(Branch.id == branch_id)
    branches = {b.id:b for b in branches.all()}
    if branch_id is not None and not branches:
        raise HTTPException(404, 'Sucursal no encontrada en esta empresa.')
    ids = list(branches)
    counts = defaultdict(lambda:dict(available=0, reserved=0, payment_pending=0, sold_net=0, incoming=0))
    for b,v,n in db.query(Inventory.branch_id,Inventory.variant_id,Inventory.stock).filter(
        Inventory.tenant_id==tenant,Inventory.branch_id.in_(ids)):
        counts[b,v]['available'] += n
    for b,v,n in db.query(Reservation.branch_id,ReservationItem.variant_id,func.sum(ReservationItem.quantity)).join(
        ReservationItem,ReservationItem.reservation_id==Reservation.id).filter(Reservation.tenant_id==tenant,
        Reservation.branch_id.in_(ids),Reservation.status.in_([ReservationStatus.pending,ReservationStatus.confirmed])
        ).group_by(Reservation.branch_id,ReservationItem.variant_id):
        counts[b,v]['reserved'] += n
    for b,v,paid,state,n in db.query(Order.branch_id,OrderItem.variant_id,Order.is_paid,Order.payment_status,func.sum(OrderItem.quantity)).join(
        OrderItem,OrderItem.order_id==Order.id).filter(Order.tenant_id==tenant,Order.branch_id.in_(ids)
        ).group_by(Order.branch_id,OrderItem.variant_id,Order.is_paid,Order.payment_status):
        if paid and state==PaymentStatusEnum.paid:
            counts[b,v]['sold_net'] += n
        elif not paid and state==PaymentStatusEnum.pending:
            counts[b,v]['payment_pending'] += n
    for ex in db.query(GarmentExchange).join(Order,Order.id==GarmentExchange.order_id).filter(
        GarmentExchange.tenant_id==tenant,GarmentExchange.branch_id.in_(ids),GarmentExchange.status=='APPROVED',
        Order.tenant_id==tenant,Order.is_paid.is_(True),Order.payment_status==PaymentStatusEnum.paid):
        counts[ex.branch_id,ex.original_variant_id]['sold_net'] -= ex.quantity
        if ex.exchange_type!='month_end_refund':
            counts[ex.branch_id,ex.new_variant_id]['sold_net'] += ex.quantity
    for b,v,n in db.query(IncomingStock.branch_id,IncomingStock.variant_id,func.sum(IncomingStock.quantity-IncomingStock.received_quantity)).filter(
        IncomingStock.tenant_id==tenant,IncomingStock.branch_id.in_(ids),IncomingStock.status.in_(['pending','partial'])
        ).group_by(IncomingStock.branch_id,IncomingStock.variant_id):
        counts[b,v]['incoming'] += n
    variants = {v.id:(v,p) for v,p in db.query(ProductVariant,Product).join(Product,ProductVariant.product_id==Product.id).filter(
        ProductVariant.tenant_id==tenant,Product.tenant_id==tenant)}
    rows=[]
    for (b,v),values in sorted(counts.items()):
        if v not in variants:
            continue
        variant,product=variants[v]
        rows.append(dict(branch_id=b,branch_name=branches[b].name,variant_id=v,sku=variant.sku,
            product_name=product.name,size=variant.size,color=variant.color,is_active=product.is_active,
            **values,physical=values['available']+values['reserved']+values['payment_pending']))
    keys=('available','reserved','payment_pending','physical','sold_net','incoming')
    return dict(generated_at=datetime.now(timezone.utc).isoformat(),count=len(rows),
        totals={key:sum(r[key] for r in rows) for key in keys},data=rows)


@router.get('/inventory')
def report(branch_id: int | None = Query(None,gt=0), db:Session=Depends(get_db),
           user=Depends(require_permission('inventory:read','inventory:write'))):
    return snapshot(db,user,branch_id)


@router.get('/inventory/export')
def export(branch_id: int | None = Query(None,gt=0), db:Session=Depends(get_db),
           user=Depends(require_permission('inventory:read','inventory:write'))):
    report=snapshot(db,user,branch_id)
    output=io.StringIO(newline='');writer=csv.writer(output)
    fields=['branch_name','sku','product_name','size','color','available','reserved','payment_pending','physical','sold_net','incoming']
    writer.writerow(['Sucursal','SKU','Producto','Talla','Color','Disponible','Reservado','Pago pendiente','Fisico','Vendido neto acumulado','Proximos ingresos'])
    for row in report['data']:
        # Prevent spreadsheet formulas from user-controlled catalog labels.
        writer.writerow([("'"+str(row[k])) if isinstance(row[k],str) and row[k].lstrip().startswith(('=','+','-','@')) else row[k] for k in fields])
    return StreamingResponse(iter([output.getvalue().encode('utf-8-sig')]),media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition':'attachment; filename=inventario.csv'})
