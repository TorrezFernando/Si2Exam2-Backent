from typing import List, Optional
from datetime import datetime, timezone
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from api.deps import get_db, get_current_active_user, get_user_permissions, require_permission
from db.models.sale import Order, OrderItem, OrderTypeEnum, PaymentStatusEnum, GarmentExchange
from db.models.tenant import Tenant
from db.models.reservation import Reservation, ReservationStatus
from db.models.operations import InventoryMovement
from schemas.sale import Order as OrderSchema, OrderCreate, GarmentExchangeCreate, GarmentExchangeSchema
from core.commerce import quantities, price_variant, move_stock, check_branch, available_branch, money
from core.access import company_id
from core.exchanges import quote as exchange_quote
from schemas.sale import GarmentExchangeQuoteRequest
import hashlib
import json

router = APIRouter()

def orders_query(db, user):
    tenant_id = company_id(user)
    query = db.query(Order).filter(Order.tenant_id == tenant_id)
    perms = get_user_permissions(db, user)
    if not perms & {'sales:read', 'sales:write'}:
        return query.filter(Order.user_id == user.id)
    if 'branches:write' not in perms:
        if not user.branch_id:
            raise HTTPException(403, 'No tienes sucursal asignada')
        query = query.filter(Order.branch_id == user.branch_id)
    return query


@router.get('', response_model=List[OrderSchema])
def list_orders(db: Session = Depends(get_db), skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100), current_user=Depends(get_current_active_user)):
    return orders_query(db, current_user).order_by(Order.id.desc()).offset(skip).limit(limit).all()

@router.get('/{order_id}', response_model=OrderSchema)
def get_order(order_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_active_user)):
    order = orders_query(db, current_user).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(404, 'Orden no encontrada')
    return order

@router.post('', response_model=OrderSchema, status_code=201)
def create_order(*, db: Session = Depends(get_db), order_in: OrderCreate,
                 current_user=Depends(get_current_active_user)):
    tenant_id = company_id(current_user)
    request_key = f'{current_user.id}:{order_in.request_key}' if order_in.request_key else None
    if request_key:
        previous = db.query(Order).filter(Order.request_key == request_key).first()
        if previous:
            return previous
    try:
        counts = quantities(order_in.items)
        perms = get_user_permissions(db, current_user)
        order_type = order_in.order_type or (OrderTypeEnum.presencial if order_in.branch_id else OrderTypeEnum.online_delivery)
        onsite = order_type != OrderTypeEnum.online_delivery
        branch_id = order_in.branch_id
        reservation = None
        customer_id = current_user.id
        reserved = {}
        if onsite:
            if 'sales:write' not in perms:
                raise HTTPException(403, 'Las ventas presenciales requieren permiso de caja')
            if not branch_id:
                raise HTTPException(400, 'Selecciona una sucursal')
            check_branch(db, current_user, branch_id)
        elif order_in.reservation_id:
            raise HTTPException(400, 'Las reservas se atienden en caja')
        if order_type == OrderTypeEnum.reserva_pickup:
            if not order_in.reservation_id:
                raise HTTPException(400, 'Indica la reserva a cobrar')
            reservation = db.query(Reservation).filter(Reservation.id == order_in.reservation_id).with_for_update().first()
            if not reservation or reservation.tenant_id != tenant_id or reservation.branch_id != branch_id:
                raise HTTPException(400, 'Reserva no encontrada en esta sucursal')
            if reservation.status not in (ReservationStatus.pending, ReservationStatus.confirmed):
                raise HTTPException(409, 'La reserva ya fue cerrada')
            reserved = quantities(reservation.items)
            if any(qty > reserved.get(vid, 0) for vid, qty in counts.items()):
                raise HTTPException(400, 'Solo puedes cobrar las cantidades reservadas')
            customer_id = reservation.user_id
        elif order_in.reservation_id:
            raise HTTPException(400, 'Tipo de venta incompatible con reserva')
        if not onsite:
            from core.libelula import ensure_configured
            ensure_configured()
            if order_in.payment_method.value not in ('tarjeta', 'qr', 'transferencia'):
                raise HTTPException(400, 'Selecciona un pago digital o realiza una reserva')
        if branch_id is None:
            branch_id = available_branch(db, counts, tenant_id)
        else:
            from db.models.branch import Branch
            branch = db.get(Branch, branch_id)
            if not branch or branch.tenant_id != tenant_id or not branch.is_active:
                raise HTTPException(400, 'Sucursal no disponible')
        res_prices = {}
        if reservation:
            res_prices = {item.variant_id: money(item.unit_price) for item in reservation.items if item.unit_price is not None}
        priced = []
        for vid, qty in counts.items():
            variant, catalog_price = price_variant(db, vid, tenant_id)
            if reservation and vid in res_prices:
                price = res_prices[vid]
            else:
                price = catalog_price
            priced.append((vid, qty, price))
        order = Order(
            tenant_id=current_user.tenant_id,
            user_id=customer_id, branch_id=branch_id, order_type=order_type,
            reservation_id=reservation.id if reservation else None, request_key=request_key,
            payment_method=order_in.payment_method,
            payment_status=PaymentStatusEnum.paid if onsite else PaymentStatusEnum.pending,
            is_paid=onsite, payment_provider='Caja_Local' if onsite else 'Libelula',
            transaction_ref=f'FS-{uuid4().hex}',
            total_amount=float(sum((qty * price for _, qty, price in priced), money(0))))
        db.add(order)
        db.flush()
        for vid, qty, price in priced:
            if not reservation:
                move_stock(db, branch_id, vid, -qty, 'sale' if onsite else 'payment_hold', current_user.id, f'order:{order.id}')
            else:
                db.add(InventoryMovement(tenant_id=current_user.tenant_id, branch_id=branch_id, variant_id=vid, quantity=0,
                    kind='reserved_sale', user_id=current_user.id, reference=f'order:{order.id};sold:{qty}'))
            db.add(OrderItem(tenant_id=current_user.tenant_id, order_id=order.id, variant_id=vid, quantity=qty, unit_price=float(price)))

        if reservation:
            for vid, qty in reserved.items():
                remainder = qty - counts.get(vid, 0)
                if remainder:
                    move_stock(db, branch_id, vid, remainder, 'reservation_release', current_user.id, f'reservation:{reservation.id}')
            reservation.status = ReservationStatus.completed
        db.commit()
        db.refresh(order)
        return order
    except IntegrityError:
        db.rollback()
        if request_key:
            previous = db.query(Order).filter(Order.request_key == request_key).first()
            if previous:
                return previous
        raise HTTPException(409, 'La operacion ya fue procesada o existe un conflicto de datos')
    except Exception:
        db.rollback()
        raise


@router.post('/{order_id}/cancel', response_model=OrderSchema)
def cancel_order(order_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_active_user)):
    """Cancela una orden online pendiente de pago y libera el stock retenido."""
    try:
        order = orders_query(db, current_user).filter(Order.id == order_id).with_for_update().first()
        if not order:
            raise HTTPException(404, 'Orden no encontrada')
        perms = get_user_permissions(db, current_user)
        is_owner = order.user_id == current_user.id
        can_staff = bool(perms & {'sales:write', 'branches:write'})
        if not is_owner and not can_staff:
            raise HTTPException(403, 'No tienes acceso a esta orden')
        if order.is_paid:
            raise HTTPException(409, 'No se puede cancelar una orden ya pagada; use devolucion')
        if order.payment_status in (PaymentStatusEnum.failed, PaymentStatusEnum.refunded):
            return order
        if order.payment_status != PaymentStatusEnum.pending:
            raise HTTPException(409, 'La orden no esta pendiente de pago')
        if order.branch_id:
            for vid, qty in quantities(order.items).items():
                move_stock(db, order.branch_id, vid, qty, 'payment_release', current_user.id, f'order:{order.id}')
        order.payment_status = PaymentStatusEnum.failed
        db.commit()
        db.refresh(order)
        return order
    except Exception:
        db.rollback()
        raise


@router.post('/{order_id}/return', response_model=OrderSchema)
def return_order(order_id: int, db: Session = Depends(get_db), current_user=Depends(require_permission('sales:write'))):
    """Full return in store; digital refunds require the gateway's refund workflow."""
    try:
        order = orders_query(db, current_user).filter(Order.id == order_id).with_for_update().first()
        if not order:
            raise HTTPException(404, 'Orden no encontrada')
        check_branch(db, current_user, order.branch_id)
        if order.payment_provider != 'Caja_Local':
            raise HTTPException(409, 'Los pagos digitales requieren tramitar el reembolso con Libelula')
        if order.payment_status == PaymentStatusEnum.refunded:
            return order
        if not order.is_paid:
            raise HTTPException(409, 'Solo puedes devolver una venta pagada')
        if db.query(GarmentExchange).filter_by(order_id=order.id, status='APPROVED').first():
            raise HTTPException(409, 'La venta tiene cambios registrados; no se puede devolver su inventario original completo.')
        for vid, qty in quantities(order.items).items():
            move_stock(db, order.branch_id, vid, qty, 'return', current_user.id, f'order:{order.id}')
        order.is_paid = False
        order.payment_status = PaymentStatusEnum.refunded
        db.commit()
        db.refresh(order)
        return order
    except Exception:
        db.rollback()
        raise


# ─── GESTIÓN DE CAMBIOS DE PRENDAS (RNF04 - FASE 2) ──────────────────────────

@router.post('/{order_id}/exchange/quote')
def quote_exchange(order_id: int, exchange_in: GarmentExchangeQuoteRequest,
                   db: Session = Depends(get_db), current_user=Depends(require_permission('sales:write'))):
    order = orders_query(db, current_user).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(404, 'Orden no encontrada')
    return exchange_quote(db, current_user, order, exchange_in)


@router.post('/{order_id}/exchange', response_model=GarmentExchangeSchema)
def exchange_garment(order_id: int, exchange_in: GarmentExchangeCreate,
                     db: Session = Depends(get_db), current_user=Depends(require_permission('sales:write'))):
    try:
        tenant_id = company_id(current_user)
        order = orders_query(db, current_user).filter(Order.id == order_id).with_for_update().first()
        if not order:
            raise HTTPException(404, 'Orden no encontrada')
        fingerprint = hashlib.sha256(json.dumps({'order_id': order_id,
            **exchange_in.model_dump(exclude={'request_key'})}, sort_keys=True).encode()).hexdigest()
        previous = db.query(GarmentExchange).filter_by(tenant_id=tenant_id, request_key=exchange_in.request_key).first()
        if previous:
            if previous.order_id != order_id or previous.request_fingerprint != fingerprint:
                raise HTTPException(409, 'La clave de solicitud ya corresponde a otro cambio o importe.')
            return previous
        result = exchange_quote(db, current_user, order, exchange_in)
        if money(exchange_in.confirmed_difference_amount) != money(result['difference_amount']):
            raise HTTPException(409, 'El precio cambió. Consulta de nuevo la diferencia antes de confirmar.')
        if not exchange_in.settlement_confirmed:
            raise HTTPException(400, 'Confirma la recepción de la prenda y el cobro o devolución de la diferencia.')
        # Deterministic inventory locking order reduces deadlocks between cashiers.
        from db.models.branch import Inventory
        db.query(Inventory).filter(Inventory.tenant_id == tenant_id,
            Inventory.branch_id == exchange_in.branch_id,
            Inventory.variant_id.in_([exchange_in.original_variant_id, exchange_in.new_variant_id])
        ).order_by(Inventory.variant_id).with_for_update().all()
        move_stock(db, exchange_in.branch_id, exchange_in.original_variant_id, exchange_in.quantity,
                   'CAMBIO_DEVOLUCION', current_user.id, f'exchange:{exchange_in.request_key}', tenant_id)
        move_stock(db, exchange_in.branch_id, exchange_in.new_variant_id, -exchange_in.quantity,
                   'CAMBIO_ENTREGA', current_user.id, f'exchange:{exchange_in.request_key}', tenant_id)
        exchange = GarmentExchange(tenant_id=tenant_id, order_id=order.id, branch_id=exchange_in.branch_id,
            user_id=current_user.id, original_variant_id=exchange_in.original_variant_id,
            new_variant_id=exchange_in.new_variant_id, quantity=exchange_in.quantity, reason=exchange_in.reason,
            difference_amount=result['difference_amount'], status='APPROVED', request_key=exchange_in.request_key,
            request_fingerprint=fingerprint, settlement_method=result['settlement_method'])
        db.add(exchange); db.commit(); db.refresh(exchange)
        return exchange
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'El cambio ya fue registrado o hay una solicitud con esa clave. Reintenta la misma solicitud.')
    except Exception:
        db.rollback()
        raise


@router.get('/{order_id}/exchanges', response_model=List[GarmentExchangeSchema])
def list_order_exchanges(
    order_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_active_user)
):
    """Lista el historial de cambios de prendas realizados sobre una orden específica."""
    order = orders_query(db, current_user).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(404, 'Orden no encontrada')

    exchanges = db.query(GarmentExchange).filter(GarmentExchange.order_id == order_id).order_by(GarmentExchange.id.desc()).all()
    tenant = order.tenant or db.query(Tenant).filter(Tenant.id == order.tenant_id).first()
    window_days = min(tenant.exchange_window_days, 2) if tenant else 2

    results = []
    for ex in exchanges:
        item = GarmentExchangeSchema.model_validate(ex)
        item.exchange_window_days = window_days
        results.append(item)
    return results


@router.get('/exchanges/all', response_model=List[GarmentExchangeSchema])
def list_all_exchanges(
    db: Session = Depends(get_db),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user = Depends(require_permission('sales:read'))
):
    """Lista todos los cambios de prendas de la empresa."""
    tenant_id = company_id(current_user)
    query = db.query(GarmentExchange).filter(GarmentExchange.tenant_id == tenant_id)
    if 'branches:write' not in get_user_permissions(db, current_user):
        check_branch(db, current_user, current_user.branch_id)
        query = query.filter(GarmentExchange.branch_id == current_user.branch_id)
    return query.order_by(GarmentExchange.id.desc()).offset(skip).limit(limit).all()



from api.routers.full_returns import router as full_returns_router
router.include_router(full_returns_router)
