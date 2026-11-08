from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from api.deps import get_db, get_current_active_user
from api.routers.orders import get_order
from core import paypal
from core.payment_gateway import gateway
from db.models.sale import Order, PaymentStatusEnum
from schemas.sale import Order as OrderSchema

router = APIRouter()


@router.post('/{order_id}/checkout')
def checkout(order_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_active_user)):
    order = get_order(order_id, db, current_user)
    # Serialize retries; use a stable merchant identifier at the provider.
    order = db.query(Order).filter(Order.id == order.id).with_for_update().populate_existing().one()
    provider = gateway(order.payment_provider)
    if order.user_id != current_user.id or not provider:
        raise HTTPException(403, 'No puedes iniciar este pago')
    info = {'provider': order.payment_provider}
    if provider is paypal:
        info.update(currency=paypal.CURRENCY, amount=float(paypal.usd_amount(order)))
    if order.is_paid:
        return {'paid': True, 'payment_url': None, **info}
    if order.payment_status != PaymentStatusEnum.pending:
        raise HTTPException(409, 'La orden no admite pagos')
    if not order.payment_id:
        order.payment_id, order.payment_url = provider.register(order)
        db.commit()
    return {'paid': False, 'payment_url': order.payment_url, **info}


def reconcile(order, db):
    provider = gateway(order.payment_provider)
    if provider and not order.is_paid and order.payment_status == PaymentStatusEnum.pending and order.payment_id:
        if provider.is_paid(order):
            order.is_paid = True
            order.payment_status = PaymentStatusEnum.paid
            db.commit()
            db.refresh(order)
    return order


@router.post('/{order_id}/verify', response_model=OrderSchema)
def verify(order_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_active_user)):
    order = get_order(order_id, db, current_user)
    return reconcile(order, db)


@router.get('/libelula/callback')
def callback(transaction_id: str, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.payment_id == transaction_id, Order.payment_provider == 'Libelula').first()
    if not order:
        raise HTTPException(404, 'Transaccion no encontrada')
    reconcile(order, db)
    if not order.is_paid:
        raise HTTPException(409, 'Pago aun no confirmado por Libelula')
    return {'ok': True}
