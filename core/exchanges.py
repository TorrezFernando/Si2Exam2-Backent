"""Authoritative quote shared by preview and atomic exchange registration."""
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from sqlalchemy import func
from core.access import company_id
from core.commerce import check_branch, money, price_variant
from db.models.sale import GarmentExchange, PaymentStatusEnum
from db.models.branch import Inventory


def utc_now():
    return datetime.now(timezone.utc)


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
    if data.original_variant_id == data.new_variant_id:
        raise HTTPException(400, 'Selecciona una variante distinta para el cambio.')
    item = next((i for i in order.items if i.variant_id == data.original_variant_id), None)
    if not item:
        raise HTTPException(400, 'La prenda original no pertenece a esta venta.')
    used = db.query(func.coalesce(func.sum(GarmentExchange.quantity), 0)).filter(
        GarmentExchange.order_id == order.id, GarmentExchange.original_variant_id == data.original_variant_id,
        GarmentExchange.status == 'APPROVED').scalar()
    if used + data.quantity > item.quantity:
        raise HTTPException(409, 'La cantidad supera las unidades compradas aún disponibles para cambio.')
    # The original product may have been deactivated; its paid line is authoritative.
    _, new_price = price_variant(db, data.new_variant_id, tenant_id)
    inventory = db.query(Inventory).filter_by(tenant_id=tenant_id, branch_id=data.branch_id, variant_id=data.new_variant_id).first()
    if not inventory or inventory.stock < data.quantity:
        raise HTTPException(409, 'Stock insuficiente para la prenda nueva.')
    paid_price = money(item.unit_price)
    difference = money((new_price - paid_price) * data.quantity)
    return dict(original_unit_price=float(paid_price), new_unit_price=float(new_price),
                quantity=data.quantity, difference_amount=float(difference),
                settlement_method='cash' if difference else 'none',
                action='collect' if difference > 0 else 'refund' if difference < 0 else 'none',
                deadline=deadline.isoformat(), exchange_window_days=days)
