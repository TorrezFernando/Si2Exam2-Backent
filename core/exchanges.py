"""Authoritative quote shared by preview and atomic exchange registration."""
import calendar
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from sqlalchemy import func
from core.access import company_id
from core.commerce import check_branch, money, price_variant
from db.models.sale import GarmentExchange, PaymentStatusEnum
from db.models.branch import Inventory


def utc_now():
    return datetime.now(timezone.utc)


def month_end_date(now=None):
    now = now or utc_now()
    last_day = calendar.monthrange(now.year, now.month)[1]
    return datetime(now.year, now.month, last_day, 23, 59, 59, tzinfo=timezone.utc)


def month_end_quote(db, data, tenant_id, paid_price, deadline, days):
    """El ítem deseado no existe: se recibe la prenda y su importe pagado se devuelve en el ajuste de fin de mes."""
    from db.models.product import ProductVariant
    desired = db.get(ProductVariant, data.new_variant_id)
    if not desired or desired.tenant_id != tenant_id:
        raise HTTPException(404, 'Variante deseada no encontrada.')
    inventory = db.query(Inventory).filter_by(tenant_id=tenant_id, branch_id=data.branch_id, variant_id=data.new_variant_id).first()
    if inventory and inventory.stock >= data.quantity:
        raise HTTPException(409, 'El ítem deseado sí tiene stock en esta sucursal. Registra el cambio normal en lugar de la devolución a fin de mes.')
    refund = money(paid_price * data.quantity)
    return dict(original_unit_price=float(paid_price), new_unit_price=0.0,
                quantity=data.quantity, difference_amount=float(-refund),
                exchange_type='month_end_refund', settlement_method='month_end',
                action='refund_month_end', refund_amount=float(refund),
                scheduled_for=month_end_date().isoformat(),
                defective=data.original_variant_id == data.new_variant_id,
                deadline=deadline.isoformat(), exchange_window_days=days)


def quote(db, user, order, data):
    if not order.is_paid or order.payment_status != PaymentStatusEnum.paid:
        raise HTTPException(400, 'Solo se admiten cambios de ventas pagadas no reembolsadas.')
    tenant_id = company_id(user)
    if order.tenant_id != tenant_id:
        raise HTTPException(404, 'Venta no encontrada.')
    check_branch(db, user, data.branch_id)
    if data.branch_id != order.branch_id:
        raise HTTPException(400, 'Realiza el cambio en la sucursal de la compra.')
    days = min(order.tenant.exchange_window_days if order.tenant else 2, 2)
    purchased = order.created_at.replace(tzinfo=order.created_at.tzinfo or timezone.utc)
    deadline = purchased + timedelta(days=days)
    if utc_now() > deadline:
        raise HTTPException(400, f'Plazo de cambio vencido. Límite: {days * 24} horas desde la compra.')
    item = next((i for i in order.items if i.variant_id == data.original_variant_id), None)
    if not item:
        raise HTTPException(400, 'La prenda original no pertenece a esta venta.')
    used = db.query(func.coalesce(func.sum(GarmentExchange.quantity), 0)).filter(
        GarmentExchange.order_id == order.id, GarmentExchange.original_variant_id == data.original_variant_id,
        GarmentExchange.status == 'APPROVED').scalar()
    if used + data.quantity > item.quantity:
        raise HTTPException(409, 'La cantidad supera las unidades compradas aún disponibles para cambio.')
    is_same_variant = data.original_variant_id == data.new_variant_id
    requested_type = getattr(data, 'exchange_type', 'variant_or_product')
    paid_price = money(item.unit_price)
    if requested_type == 'month_end_refund':
        return month_end_quote(db, data, tenant_id, paid_price, deadline, days)
    exchange_type = 'same_defective' if is_same_variant else requested_type
    if is_same_variant:
        # Cambio por el mismo producto (prenda defectuosa): mantiene el mismo precio pagado sin cobro adicional.
        from db.models.product import ProductVariant
        variant = db.get(ProductVariant, data.new_variant_id)
        if not variant or variant.tenant_id != tenant_id:
            raise HTTPException(404, 'Variante no encontrada.')
        new_price = paid_price
    else:
        # The original product may have been deactivated; its paid line is authoritative.
        _, new_price = price_variant(db, data.new_variant_id, tenant_id)
    inventory = db.query(Inventory).filter_by(tenant_id=tenant_id, branch_id=data.branch_id, variant_id=data.new_variant_id).first()
    if not inventory or inventory.stock < data.quantity:
        raise HTTPException(
            409,
            'Stock insuficiente para la prenda solicitada. Si no existe el ítem deseado, puedes elegir otro producto (registrando el monto adicional si supera el precio) o procesar la devolución del dinero a Fin de Mes.'
        )
    difference = money((new_price - paid_price) * data.quantity)
    return dict(original_unit_price=float(paid_price), new_unit_price=float(new_price),
                quantity=data.quantity, difference_amount=float(difference),
                exchange_type=exchange_type,
                settlement_method='cash' if difference else 'none',
                action='collect' if difference > 0 else 'refund' if difference < 0 else 'none',
                deadline=deadline.isoformat(), exchange_window_days=days)
