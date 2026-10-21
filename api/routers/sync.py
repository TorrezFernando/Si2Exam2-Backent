import logging
from typing import List, Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import func

from api.deps import get_db, get_current_active_user, get_user_permissions, require_permission
from db.models.user import User
from db.models.tenant import Tenant
from db.models.branch import Branch, Inventory
from db.models.product import Product, ProductVariant, Category
from db.models.sale import Order, OrderItem, OrderTypeEnum, PaymentStatusEnum, PaymentMethodEnum
from db.models.reservation import Reservation, ReservationItem, ReservationStatus
from core.commerce import move_stock, price_variant, quantities
from schemas.sync import (
    SyncCatalogResponse,
    SyncProductItem,
    SyncVariantItem,
    SyncCategoryItem,
    SyncCustomerItem,
    SyncBatchRequest,
    SyncBatchResponse,
    SyncOperationReceipt,
)

logger = logging.getLogger("offline_sync")
logger.setLevel(logging.INFO)

router = APIRouter()


# ─── 1. DESCARGA DE CATÁLOGO OFFLINE ─────────────────────────────────────────

@router.get("/catalog", response_model=SyncCatalogResponse)
def get_offline_catalog(
    branch_id: Optional[int] = Query(None, description="ID de la sucursal para consultar inventario"),
    last_sync_at: Optional[datetime] = Query(None, description="Timestamp de última sincronización local"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    RNF03 / Fase 2: Descarga optimizada del catálogo de productos, variantes con existencias
    específicas de la sucursal autorizada, categorías y clientes mínimos para operación offline.
    """
    from core.offline_sync import context
    target = branch_id or current_user.branch_id
    if not target:
        raise HTTPException(400, 'Selecciona una sucursal autorizada.')
    tenant_id, perms = context(db, current_user, target)

    # 1. Determinar Tenant
    tenant_id = current_user.tenant_id
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first() if tenant_id else None
    exchange_window_days = tenant.exchange_window_days if tenant else 30

    # 2. Determinar Sucursal
    target_branch_id = branch_id or current_user.branch_id
    if not target_branch_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debe especificar una sucursal o el usuario debe tener una asignada.",
        )

    branch = db.query(Branch).filter(Branch.id == target_branch_id).first()
    if not branch or not branch.is_active:
        raise HTTPException(status_code=404, detail="Sucursal no encontrada o inactiva.")

    if tenant_id is not None and branch.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="La sucursal no pertenece a su empresa.")

    if "branches:write" not in perms and current_user.branch_id and current_user.branch_id != target_branch_id:
        raise HTTPException(status_code=403, detail="Solo puede sincronizar el catálogo de su sucursal asignada.")

    # 3. Categorías
    cat_query = db.query(Category)
    if tenant_id is not None:
        cat_query = cat_query.filter((Category.tenant_id == tenant_id) | (Category.tenant_id.is_(None)))
    categories = [
        SyncCategoryItem(id=c.id, name=c.name, description=c.description)
        for c in cat_query.all()
    ]

    # 4. Inventario de la Sucursal (Map variant_id -> stock)
    inv_map = {
        inv.variant_id: inv.stock
        for inv in db.query(Inventory).filter(Inventory.branch_id == target_branch_id).all()
    }

    # 5. Productos y Variantes
    prod_query = db.query(Product).filter(Product.is_active.is_(True))
    if tenant_id is not None:
        prod_query = prod_query.filter(Product.tenant_id == tenant_id)

    products_result = []
    for p in prod_query.all():
        sync_variants = []
        for v in p.variants:
            stock = inv_map.get(v.id, 0)
            sync_variants.append(
                SyncVariantItem(
                    id=v.id,
                    sku=v.sku,
                    size=v.size,
                    color=v.color,
                    price=float(v.price_override if v.price_override is not None else p.base_price),
                    stock=max(0, stock),
                    image_url=v.image_url,
                )
            )

        products_result.append(
            SyncProductItem(
                id=p.id,
                name=p.name,
                description=p.description,
                category_id=p.category_id,
                category_name=p.category.name if p.category else None,
                base_price=p.base_price,
                image_url=p.image_url,
                variants=sync_variants,
            )
        )

    # 6. Clientes mínimos (solo para personal con permisos de venta o lectura de usuarios)
    is_admin = False
    customers = []
    if is_admin or (perms & {"sales:write", "users:read"}):
        cust_query = db.query(User).filter(User.is_active.is_(True))
        if tenant_id is not None:
            cust_query = cust_query.filter(User.tenant_id == tenant_id)
        # Solo clientes (no personal ni administradores para no exponer datos de staff a caja básica)
        if hasattr(User, "role_id"):
            cust_query = cust_query.join(User.role, isouter=True).filter(
                (User.role.has(name="cliente")) | (User.role_id.is_(None))
            )
        customers = [
            SyncCustomerItem(
                id=u.id,
                full_name=u.full_name or u.email,
                email=u.email,
                phone=u.phone,
            )
            for u in cust_query.limit(200).all()
        ]

    logger.info(
        f"[RNF10-SYNC][CATALOG] Tenant={tenant_id} Branch={target_branch_id} "
        f"Products={len(products_result)} Customers={len(customers)}"
    )

    return SyncCatalogResponse(
        tenant_id=tenant_id or 1,
        branch_id=target_branch_id,
        branch_name=branch.name,
        server_timestamp=datetime.now(timezone.utc),
        exchange_window_days=exchange_window_days,
        categories=categories,
        products=products_result,
        customers=customers,
    )


# ─── 2. SINCRONIZACIÓN POR LOTES (VENTAS Y RESERVAS OFFLINE) ──────────────────

@router.post("/operations", response_model=SyncBatchResponse)
def sync_offline_operations(
    batch: SyncBatchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    from core.offline_sync import process_batch
    return process_batch(batch, db, current_user)
