import calendar
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from api.deps import get_db, require_permission, get_user_permissions
from core.access import company_id
from core.commerce import money, check_branch, move_stock
from db.models.sale import GarmentExchange, PaymentStatusEnum, PaymentMethodEnum
from db.models.product import ProductVariant
from db.models.branch import Inventory
from db.models.operations import InventoryMovement
from db.models.full_return import FullReturn

router=APIRouter()


def utc_now():
    return datetime.now(timezone.utc)


def month_end_date(dt: Optional[datetime] = None) -> datetime:
    now = dt or utc_now()
    last_day = calendar.monthrange(now.year, now.month)[1]
    return datetime(now.year, now.month, last_day, 23, 59, 59, tzinfo=timezone.utc)


class ReturnInput(BaseModel):
    request_key: str = Field(min_length=10,max_length=64)
    confirmed_refund_amount: float = Field(ge=0,allow_inf_nan=False)
    settlement_confirmed: bool = False
    refund_timing: str = Field('IMMEDIATE', pattern='^(IMMEDIATE|MONTH_END)$')
    return_reason: Optional[str] = Field(default=None, max_length=255)
    is_defective: bool = False
    desired_variant_id: Optional[int] = Field(default=None, gt=0)


def get_order(db,user,order_id,lock=False):
    from api.routers.orders import orders_query
    query=orders_query(db,user).filter_by(id=order_id,tenant_id=company_id(user))
    if lock: query=query.with_for_update()
    order=query.first()
    if not order: raise HTTPException(404,'Venta no encontrada.')
    check_branch(db,user,order.branch_id)
    return order


def quote(db,order,refund_timing='IMMEDIATE'):
    if refund_timing == 'IMMEDIATE':
        if order.payment_provider not in ('Caja_Local','Caja_Offline') or order.payment_method!=PaymentMethodEnum.efectivo:
            raise HTTPException(409,'Este flujo devuelve efectivo cobrado en caja. Si no existe el ítem deseado o fue pago digital, selecciona Devolución a Fin de Mes (Ajuste Mensual).')
    if not order.is_paid or order.payment_status!=PaymentStatusEnum.paid:
        raise HTTPException(409,'La venta no está pagada o ya fue devuelta.')
    held=defaultdict(int)
    for item in order.items: held[item.variant_id]+=item.quantity
    amount=money(order.total_amount)
    exchanges=db.query(GarmentExchange).filter_by(tenant_id=order.tenant_id,order_id=order.id,status='APPROVED').order_by(GarmentExchange.id).all()
    for exchange in exchanges:
        if exchange.settlement_method not in ('cash','none','month_end') or (exchange.settlement_method=='none' and money(exchange.difference_amount)!=0):
            raise HTTPException(409,'Hay cambios sin liquidación documentada. Requieren conciliación antes de devolver dinero.')
        held[exchange.original_variant_id]-=exchange.quantity
        # month_end: la prenda se recibió y su importe ya está comprometido para fin de mes; no se entregó otra.
        if exchange.settlement_method!='month_end':
            held[exchange.new_variant_id]+=exchange.quantity
        amount+=money(exchange.difference_amount)
        if held[exchange.original_variant_id]<0:
            raise HTTPException(409,'El historial de prendas requiere conciliación.')
    if amount<0 or not any(held.values()):
        raise HTTPException(409,'El saldo de la venta requiere conciliación.')
    items=[]
    for variant_id,quantity in sorted(held.items()):
        if not quantity: continue
        variant=db.get(ProductVariant,variant_id)
        if not variant or variant.tenant_id!=order.tenant_id or variant.product.tenant_id!=order.tenant_id:
            raise HTTPException(409,'El historial contiene una prenda no disponible para esta empresa.')
        items.append(dict(variant_id=variant_id,quantity=quantity,label=f'{variant.product.name} · {variant.size} / {variant.color}'))
    return dict(
        order_id=order.id,
        branch_id=order.branch_id,
        refund_amount=float(amount),
        items=items,
        refund_timing=refund_timing,
        scheduled_for=month_end_date().isoformat() if refund_timing == 'MONTH_END' else None,
    )


def receipt(row):
    return dict(
        id=row.id,
        order_id=row.order_id,
        branch_id=row.branch_id,
        refund_amount=row.refund_amount,
        items=row.items,
        refund_timing=row.refund_timing,
        disbursement_status=row.disbursement_status,
        scheduled_for=row.scheduled_for,
        settled_at=row.settled_at,
        return_reason=row.return_reason,
        is_defective=row.is_defective,
        desired_variant_id=row.desired_variant_id,
        created_at=row.created_at,
    )


def scoped(db,user,query,model):
    if 'branches:write' not in get_user_permissions(db,user):
        if not user.branch_id:
            raise HTTPException(403,'No tienes sucursal asignada.')
        query=query.filter(model.branch_id==user.branch_id)
    return query


def month_end_exchanges(db,user):
    query=db.query(GarmentExchange).filter(GarmentExchange.tenant_id==company_id(user),
        GarmentExchange.status=='APPROVED',GarmentExchange.settlement_method=='month_end')
    return scoped(db,user,query,GarmentExchange)


def exchange_row(ex):
    """Devolución de una sola prenda porque el ítem deseado no existía (registrada desde Cambios)."""
    return dict(source='exchange',id=ex.id,order_id=ex.order_id,branch_id=ex.branch_id,
        refund_amount=float(-money(ex.difference_amount)),quantity=ex.quantity,
        original_variant_id=ex.original_variant_id,desired_variant_id=ex.new_variant_id,
        refund_timing='MONTH_END',disbursement_status=ex.disbursement_status,
        scheduled_for=ex.scheduled_for,settled_at=ex.settled_at,return_reason=ex.reason,
        is_defective=ex.original_variant_id==ex.new_variant_id,created_at=ex.created_at)


@router.get('/full-returns/month-end')
def list_month_end_returns(db:Session=Depends(get_db),user=Depends(require_permission('sales:read','sales:write'))):
    tenant=company_id(user)
    query=scoped(db,user,db.query(FullReturn).filter(FullReturn.tenant_id==tenant,FullReturn.refund_timing=='MONTH_END'),FullReturn)
    rows=[dict(receipt(r),source='full_return') for r in query.all()]
    rows+=[exchange_row(ex) for ex in month_end_exchanges(db,user).all()]
    return sorted(rows,key=lambda r:r['created_at'],reverse=True)


@router.post('/full-returns/process-month-end')
def process_all_month_end_returns(db:Session=Depends(get_db),user=Depends(require_permission('sales:write','branches:write'))):
    tenant=company_id(user)
    returns=scoped(db,user,db.query(FullReturn).filter(
        FullReturn.tenant_id==tenant,
        FullReturn.refund_timing=='MONTH_END',
        FullReturn.disbursement_status=='PENDING_MONTH_END',
    ),FullReturn).with_for_update().all()
    exchanges=month_end_exchanges(db,user).filter(GarmentExchange.disbursement_status=='PENDING_MONTH_END').with_for_update().all()
    now=utc_now()
    for r in [*returns,*exchanges]:
        r.disbursement_status='COMPLETED'
        r.settled_at=now
    total=sum((money(r.refund_amount) for r in returns),money(0))+sum((-money(e.difference_amount) for e in exchanges),money(0))
    db.commit()
    return dict(processed_count=len(returns)+len(exchanges),total_settled=float(total))


@router.post('/exchanges/{exchange_id}/settle-month-end')
def settle_exchange_month_end(exchange_id:int,db:Session=Depends(get_db),user=Depends(require_permission('sales:write','branches:write'))):
    row=month_end_exchanges(db,user).filter(GarmentExchange.id==exchange_id).with_for_update().first()
    if not row:
        raise HTTPException(404,'No existe devolución de fin de mes para este cambio.')
    if row.disbursement_status!='COMPLETED':
        row.disbursement_status='COMPLETED'
        row.settled_at=utc_now()
        db.commit()
        db.refresh(row)
    return exchange_row(row)


@router.post('/{order_id}/full-return/settle-month-end')
def settle_single_month_end_return(order_id:int,db:Session=Depends(get_db),user=Depends(require_permission('sales:write','branches:write'))):
    order=get_order(db,user,order_id,lock=True)
    row=db.query(FullReturn).filter_by(order_id=order.id,tenant_id=order.tenant_id).with_for_update().first()
    if not row:
        raise HTTPException(404,'No existe devolución registrada para esta venta.')
    if row.disbursement_status != 'COMPLETED':
        row.disbursement_status='COMPLETED'
        row.settled_at=utc_now()
        db.commit()
        db.refresh(row)
    return receipt(row)


@router.get('/{order_id}/full-return')
def existing(order_id:int,db:Session=Depends(get_db),user=Depends(require_permission('sales:write'))):
    order=get_order(db,user,order_id)
    row=db.query(FullReturn).filter_by(order_id=order.id,tenant_id=order.tenant_id).first()
    return receipt(row) if row else None


@router.post('/{order_id}/full-return/quote')
def preview(order_id:int,db:Session=Depends(get_db),user=Depends(require_permission('sales:write')),
            refund_timing:str=Query('IMMEDIATE',pattern='^(IMMEDIATE|MONTH_END)$')):
    timing=refund_timing if isinstance(refund_timing,str) else 'IMMEDIATE'
    return quote(db,get_order(db,user,order_id),timing)


@router.post('/{order_id}/full-return')
def create(order_id:int,data:ReturnInput,db:Session=Depends(get_db),user=Depends(require_permission('sales:write'))):
    try:
        order=get_order(db,user,order_id,lock=True)
        previous=db.query(FullReturn).filter_by(tenant_id=order.tenant_id,request_key=data.request_key).first()
        if previous:
            if previous.order_id!=order.id or money(previous.refund_amount)!=money(data.confirmed_refund_amount) or not data.settlement_confirmed:
                raise HTTPException(409,'La clave ya corresponde a otra devolución o importe.')
            return receipt(previous)
        if db.query(FullReturn).filter_by(order_id=order.id).first():
            raise HTTPException(409,'Esta venta ya tiene un comprobante de devolución. Consulta el comprobante existente.')
        timing=data.refund_timing if data.refund_timing in ('IMMEDIATE','MONTH_END') else 'IMMEDIATE'
        result=quote(db,order,timing)
        if not data.settlement_confirmed or money(result['refund_amount'])!=money(data.confirmed_refund_amount):
            raise HTTPException(409,'Confirma la recepción de todas las prendas y el importe actualizado antes de registrar.')
        ids=[item['variant_id'] for item in result['items']]
        db.query(Inventory).filter(Inventory.tenant_id==order.tenant_id,Inventory.branch_id==order.branch_id,
            Inventory.variant_id.in_(ids)).order_by(Inventory.variant_id).with_for_update().all()
        for item in result['items']:
            if data.is_defective:
                db.add(InventoryMovement(
                    tenant_id=order.tenant_id,
                    branch_id=order.branch_id,
                    variant_id=item['variant_id'],
                    quantity=0,
                    kind='full_return_defective',
                    user_id=user.id,
                    reference=f'full_return:{data.request_key};defective:{item["quantity"]}',
                ))
            else:
                move_stock(db,order.branch_id,item['variant_id'],item['quantity'],'full_return',user.id,f'full_return:{data.request_key}')
        now=utc_now()
        row=FullReturn(
            tenant_id=order.tenant_id,
            order_id=order.id,
            branch_id=order.branch_id,
            user_id=user.id,
            request_key=data.request_key,
            refund_amount=result['refund_amount'],
            items=result['items'],
            refund_timing=timing,
            disbursement_status='PENDING_MONTH_END' if timing=='MONTH_END' else 'COMPLETED',
            scheduled_for=month_end_date(now) if timing=='MONTH_END' else now,
            settled_at=None if timing=='MONTH_END' else now,
            return_reason=data.return_reason or ('Devolución programada a fin de mes por inexistencia del ítem deseado' if timing=='MONTH_END' else 'Devolución en caja'),
            is_defective=bool(data.is_defective),
            desired_variant_id=data.desired_variant_id,
        )
        db.add(row)
        order.is_paid=False;order.payment_status=PaymentStatusEnum.refunded
        db.commit();db.refresh(row)
        return receipt(row)
    except IntegrityError:
        db.rollback()
        raise HTTPException(503,'Solicitud concurrente. Reintenta la misma devolución para recuperar su comprobante.')
    except Exception:
        db.rollback()
        raise
