from collections import defaultdict
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from api.deps import get_db, require_permission
from core.access import company_id
from core.commerce import money, check_branch, move_stock
from db.models.sale import GarmentExchange, PaymentStatusEnum, PaymentMethodEnum
from db.models.product import ProductVariant
from db.models.branch import Inventory
from db.models.full_return import FullReturn

router=APIRouter()


class ReturnInput(BaseModel):
    request_key: str = Field(min_length=10,max_length=64)
    confirmed_refund_amount: float = Field(ge=0,allow_inf_nan=False)
    settlement_confirmed: bool = False


def get_order(db,user,order_id,lock=False):
    from api.routers.orders import orders_query
    query=orders_query(db,user).filter_by(id=order_id,tenant_id=company_id(user))
    if lock: query=query.with_for_update()
    order=query.first()
    if not order: raise HTTPException(404,'Venta no encontrada.')
    check_branch(db,user,order.branch_id)
    return order


def quote(db,order):
    if order.payment_provider not in ('Caja_Local','Caja_Offline') or order.payment_method!=PaymentMethodEnum.efectivo:
        raise HTTPException(409,'Este flujo devuelve efectivo cobrado en caja. Los pagos digitales requieren el reembolso de su proveedor.')
    if not order.is_paid or order.payment_status!=PaymentStatusEnum.paid:
        raise HTTPException(409,'La venta no está pagada o ya fue devuelta.')
    held=defaultdict(int)
    for item in order.items: held[item.variant_id]+=item.quantity
    amount=money(order.total_amount)
    exchanges=db.query(GarmentExchange).filter_by(tenant_id=order.tenant_id,order_id=order.id,status='APPROVED').order_by(GarmentExchange.id).all()
    for exchange in exchanges:
        if exchange.settlement_method not in ('cash','none') or (exchange.settlement_method=='none' and money(exchange.difference_amount)!=0):
            raise HTTPException(409,'Hay cambios sin liquidación documentada. Requieren conciliación antes de devolver dinero.')
        held[exchange.original_variant_id]-=exchange.quantity
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
    return dict(order_id=order.id,branch_id=order.branch_id,refund_amount=float(amount),items=items)


def receipt(row):
    return dict(id=row.id,order_id=row.order_id,branch_id=row.branch_id,refund_amount=row.refund_amount,items=row.items,created_at=row.created_at)


@router.get('/{order_id}/full-return')
def existing(order_id:int,db:Session=Depends(get_db),user=Depends(require_permission('sales:write'))):
    order=get_order(db,user,order_id)
    row=db.query(FullReturn).filter_by(order_id=order.id,tenant_id=order.tenant_id).first()
    return receipt(row) if row else None


@router.post('/{order_id}/full-return/quote')
def preview(order_id:int,db:Session=Depends(get_db),user=Depends(require_permission('sales:write'))):
    return quote(db,get_order(db,user,order_id))


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
        result=quote(db,order)
        if not data.settlement_confirmed or money(result['refund_amount'])!=money(data.confirmed_refund_amount):
            raise HTTPException(409,'Confirma la recepción de todas las prendas y el importe actualizado antes de registrar.')
        ids=[item['variant_id'] for item in result['items']]
        db.query(Inventory).filter(Inventory.tenant_id==order.tenant_id,Inventory.branch_id==order.branch_id,
            Inventory.variant_id.in_(ids)).order_by(Inventory.variant_id).with_for_update().all()
        for item in result['items']:
            move_stock(db,order.branch_id,item['variant_id'],item['quantity'],'full_return',user.id,f'full_return:{data.request_key}')
        row=FullReturn(tenant_id=order.tenant_id,order_id=order.id,branch_id=order.branch_id,user_id=user.id,
            request_key=data.request_key,refund_amount=result['refund_amount'],items=result['items'])
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
