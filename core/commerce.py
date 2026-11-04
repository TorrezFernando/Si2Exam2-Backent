"""Shared transactional stock operations. Callers commit once, or roll back."""
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from fastapi import HTTPException
from sqlalchemy import update
from db.models.branch import Branch, Inventory
from db.models.product import ProductVariant
from db.models.operations import InventoryMovement
from api.deps import get_user_permissions
from core.access import company_id
from db.models.user import User


def money(value):
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def quantities(items):
    result = defaultdict(int)
    for item in items:
        result[item.variant_id] += item.quantity
    return dict(sorted(result.items()))


def check_branch(db, user, branch_id):
    branch = db.get(Branch, branch_id)
    if not branch or branch.tenant_id != company_id(user) or not branch.is_active:
        raise HTTPException(400, 'Sucursal no disponible')
    # Global administration requires the effective branches permission.
    if 'branches:write' not in get_user_permissions(db, user):
        if user.branch_id != branch_id:
            raise HTTPException(403, 'Solo puedes operar en tu sucursal asignada')
    return branch


def price_variant(db, variant_id, tenant_id):
    variant = db.get(ProductVariant, variant_id)
    if not variant or tenant_id is None or variant.tenant_id != tenant_id or variant.product.tenant_id != tenant_id or not variant.product.is_active:
        raise HTTPException(400, f'Variante {variant_id} no disponible')
    return variant, money(variant.effective_price)


def move_stock(db, branch_id, variant_id, delta, kind, user_id, reference, tenant_id=None):
    """Atomic conditional update prevents overselling, including on SQLite."""
    actor = db.get(User, user_id)
    if not actor:
        raise HTTPException(403, 'Usuario no disponible.')
    actor_tenant = company_id(actor)
    if tenant_id is not None and tenant_id != actor_tenant:
        raise HTTPException(403, 'Empresa incompatible con el usuario.')
    tenant_id = actor_tenant
    branch = db.get(Branch, branch_id)
    variant = db.get(ProductVariant, variant_id)
    if not branch or branch.tenant_id != tenant_id or not variant or variant.tenant_id != tenant_id or variant.product.tenant_id != tenant_id:
        raise HTTPException(403, 'Inventario ajeno a la empresa.')
    statement = update(Inventory).where(
        Inventory.branch_id == branch_id, Inventory.variant_id == variant_id, Inventory.tenant_id == tenant_id)
    if delta < 0:
        statement = statement.where(Inventory.stock >= -delta)
    result = db.execute(statement.values(stock=Inventory.stock + delta),
                        execution_options={'synchronize_session': False})
    if result.rowcount != 1:
        raise HTTPException(409, f'Stock insuficiente o inventario inexistente: variante {variant_id}, sucursal {branch_id}')
    db.add(InventoryMovement(tenant_id=tenant_id, branch_id=branch_id, variant_id=variant_id,
        quantity=delta, kind=kind, user_id=user_id, reference=reference))


def available_branch(db, counts, tenant_id):
    if tenant_id is None:
        raise HTTPException(403, 'Selecciona una empresa.')
    for branch in db.query(Branch).filter(Branch.is_active.is_(True), Branch.tenant_id == tenant_id).order_by(Branch.id):
        stock = {i.variant_id: i.stock for i in db.query(Inventory).filter(Inventory.branch_id == branch.id)}
        if all(stock.get(vid, 0) >= qty for vid, qty in counts.items()):
            return branch.id
    raise HTTPException(409, 'Ninguna sucursal dispone de todas las prendas solicitadas')
