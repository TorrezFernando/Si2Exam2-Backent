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
    perms = get_user_permissions(db, current_user)

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
    is_admin = bool(current_user.role and current_user.role.name in ("superadmin", "administrador"))
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
    """
    RNF04 / RNF05 / RNF10 / Fase 2: Procesa en lote operaciones registradas sin conexión
    (ventas de contado y reservas) con bloqueo pesimista de inventario e idempotencia estricta.
    Retorna recibo individual para cada operación (CONFIRMADA o RECHAZADA_CONFLICTO).
    """
    perms = get_user_permissions(db, current_user)
    is_admin = bool(current_user.role and current_user.role.name in ("superadmin", "administrador"))
    
    # Validación estricta de permisos según los tipos de operación presentes en el lote
    if batch.sales and not (is_admin or "sales:write" in perms):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permisos insuficientes para sincronizar ventas de caja.",
        )
    if batch.reservations and not (is_admin or "reservations:write" in perms):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permisos insuficientes para sincronizar reservas.",
        )

    tenant_id = current_user.tenant_id

    # 1. Validar Sucursal
    branch = db.query(Branch).filter(Branch.id == batch.branch_id).first()
    if not branch or not branch.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Sucursal {batch.branch_id} inexistente o inactiva.",
        )
    if tenant_id is not None and branch.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="La sucursal no pertenece a su empresa.",
        )
    if "branches:write" not in perms and current_user.branch_id and current_user.branch_id != batch.branch_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo puede sincronizar operaciones de su sucursal asignada.",
        )

    receipts: List[SyncOperationReceipt] = []
    confirmed_count = 0
    conflict_count = 0

    logger.info(
        f"[RNF10-SYNC][BATCH_START] Tenant={tenant_id} Branch={batch.branch_id} "
        f"Sales={len(batch.sales)} Reservations={len(batch.reservations)}"
    )

    # ─── A. Procesar Ventas Offline ──────────────────────────────────────────
    for sale_in in batch.sales:
        uuid = sale_in.client_operation_uuid

        # Idempotencia: Verificar si ya existe en este tenant
        existing_order_query = db.query(Order).filter(Order.client_operation_uuid == uuid)
        if tenant_id is not None:
            existing_order_query = existing_order_query.filter(Order.tenant_id == tenant_id)
        existing_order = existing_order_query.first()

        if existing_order:
            logger.info(
                f"[RNF10-SYNC][IDEMPOTENT_SALE] UUID={uuid} ya procesada. OrderID={existing_order.id}"
            )
            receipts.append(
                SyncOperationReceipt(
                    operation_type="sale",
                    client_operation_uuid=uuid,
                    status=existing_order.sync_status,
                    server_id=existing_order.id,
                    rejection_reason=existing_order.rejection_reason,
                    processed_at=datetime.now(timezone.utc),
                )
            )
            if existing_order.sync_status == "CONFIRMADA":
                confirmed_count += 1
            else:
                conflict_count += 1
            continue

        # 0. Validar método de pago: solo efectivo/cash para offline sync
        payment_method_raw = str(sale_in.payment_method).lower()
        if not ("efectivo" in payment_method_raw or "cash" in payment_method_raw):
            conflict_msg = f"Método de pago '{sale_in.payment_method}' no permitido para sincronización offline. Solo se admite efectivo."
            logger.warning(f"[RNF10-SYNC][SALE_PAYMENT_CONFLICT] UUID={uuid}: {conflict_msg}")
            try:
                conflict_order = Order(
                    tenant_id=tenant_id,
                    user_id=current_user.id,
                    branch_id=batch.branch_id,
                    order_type=OrderTypeEnum.presencial,
                    payment_method=PaymentMethodEnum.efectivo,
                    payment_status=PaymentStatusEnum.failed,
                    is_paid=False,
                    payment_provider="Caja_Offline",
                    transaction_ref=f"FS-OFF-FAIL-{uuid[:8]}",
                    total_amount=0.0,
                    client_operation_uuid=uuid,
                    is_offline_operation=True,
                    sync_status="RECHAZADA_CONFLICTO",
                    rejection_reason=conflict_msg,
                    created_at=sale_in.created_at or datetime.now(timezone.utc),
                )
                db.add(conflict_order)
                db.commit()
            except Exception:
                db.rollback()

            conflict_count += 1
            receipts.append(
                SyncOperationReceipt(
                    operation_type="sale",
                    client_operation_uuid=uuid,
                    status="RECHAZADA_CONFLICTO",
                    server_id=None,
                    rejection_reason=conflict_msg,
                    processed_at=datetime.now(timezone.utc),
                )
            )
            continue

        # 0.1 Validar pertenencia del cliente al tenant si se especifica user_id
        customer_id = current_user.id
        if sale_in.user_id:
            cust_q = db.query(User).filter(User.id == sale_in.user_id, User.is_active.is_(True))
            if tenant_id is not None:
                cust_q = cust_q.filter(User.tenant_id == tenant_id)
            cust = cust_q.first()
            if not cust:
                conflict_msg = f"Cliente con ID {sale_in.user_id} no existe o no pertenece a la empresa {tenant_id}."
                logger.warning(f"[RNF10-SYNC][SALE_CUST_CONFLICT] UUID={uuid}: {conflict_msg}")
                try:
                    conflict_order = Order(
                        tenant_id=tenant_id,
                        user_id=current_user.id,
                        branch_id=batch.branch_id,
                        order_type=OrderTypeEnum.presencial,
                        payment_method=PaymentMethodEnum.efectivo,
                        payment_status=PaymentStatusEnum.failed,
                        is_paid=False,
                        payment_provider="Caja_Offline",
                        transaction_ref=f"FS-OFF-FAIL-{uuid[:8]}",
                        total_amount=0.0,
                        client_operation_uuid=uuid,
                        is_offline_operation=True,
                        sync_status="RECHAZADA_CONFLICTO",
                        rejection_reason=conflict_msg,
                        created_at=sale_in.created_at or datetime.now(timezone.utc),
                    )
                    db.add(conflict_order)
                    db.commit()
                except Exception:
                    db.rollback()

                conflict_count += 1
                receipts.append(
                    SyncOperationReceipt(
                        operation_type="sale",
                        client_operation_uuid=uuid,
                        status="RECHAZADA_CONFLICTO",
                        server_id=None,
                        rejection_reason=conflict_msg,
                        processed_at=datetime.now(timezone.utc),
                    )
                )
                continue
            customer_id = cust.id

        # Procesamiento en subtransacción atómica aislada
        savepoint = db.begin_nested()
        try:
            # 1. Bloqueo pesimista e inspección de stock para cada variante
            counts = quantities(sale_in.items)
            conflict_detected = False
            conflict_msg = ""

            for vid, req_qty in counts.items():
                inv = (
                    db.query(Inventory)
                    .filter(Inventory.branch_id == batch.branch_id, Inventory.variant_id == vid)
                    .with_for_update()
                    .first()
                )
                available_stock = inv.stock if inv else 0
                if not inv or inv.stock < req_qty:
                    conflict_detected = True
                    conflict_msg = (
                        f"Stock insuficiente para variante {vid} en sucursal {batch.branch_id}. "
                        f"Solicitado: {req_qty}, Disponible central: {available_stock}."
                    )
                    break

            if conflict_detected:
                savepoint.rollback()
                conflict_count += 1
                logger.warning(f"[RNF10-SYNC][SALE_CONFLICT] UUID={uuid}: {conflict_msg}")
                # Persistir conflicto para idempotencia
                try:
                    conflict_order = Order(
                        tenant_id=tenant_id,
                        user_id=customer_id,
                        branch_id=batch.branch_id,
                        order_type=OrderTypeEnum.presencial,
                        payment_method=PaymentMethodEnum.efectivo,
                        payment_status=PaymentStatusEnum.failed,
                        is_paid=False,
                        payment_provider="Caja_Offline",
                        transaction_ref=f"FS-OFF-FAIL-{uuid[:8]}",
                        total_amount=0.0,
                        client_operation_uuid=uuid,
                        is_offline_operation=True,
                        sync_status="RECHAZADA_CONFLICTO",
                        rejection_reason=conflict_msg,
                        created_at=sale_in.created_at or datetime.now(timezone.utc),
                    )
                    db.add(conflict_order)
                    db.commit()
                except Exception:
                    db.rollback()

                receipts.append(
                    SyncOperationReceipt(
                        operation_type="sale",
                        client_operation_uuid=uuid,
                        status="RECHAZADA_CONFLICTO",
                        server_id=None,
                        rejection_reason=conflict_msg,
                        processed_at=datetime.now(timezone.utc),
                    )
                )
                continue

            # 2. Descuento de inventario y generación de orden
            priced = []
            for vid, qty in counts.items():
                _, price = price_variant(db, vid)
                move_stock(
                    db=db,
                    branch_id=batch.branch_id,
                    variant_id=vid,
                    delta=-qty,
                    kind="offline_sale",
                    user_id=current_user.id,
                    reference=f"sync_sale:{uuid}",
                    tenant_id=tenant_id,
                )
                priced.append((vid, qty, float(price)))

            server_calculated_total = round(sum(q * p for _, q, p in priced), 2)

            order = Order(
                tenant_id=tenant_id,
                user_id=customer_id,
                branch_id=batch.branch_id,
                order_type=OrderTypeEnum.presencial,
                payment_method=PaymentMethodEnum.efectivo,
                payment_status=PaymentStatusEnum.paid,
                is_paid=True,
                payment_provider="Caja_Offline",
                transaction_ref=f"FS-OFF-{uuid[:8]}",
                total_amount=server_calculated_total,
                client_operation_uuid=uuid,
                is_offline_operation=True,
                sync_status="CONFIRMADA",
                created_at=sale_in.created_at or datetime.now(timezone.utc),
            )
            db.add(order)
            db.flush()

            for vid, qty, price in priced:
                db.add(
                    OrderItem(
                        tenant_id=tenant_id,
                        order_id=order.id,
                        variant_id=vid,
                        quantity=qty,
                        unit_price=price,
                    )
                )

            savepoint.commit()
            confirmed_count += 1
            logger.info(f"[RNF10-SYNC][SALE_CONFIRMED] UUID={uuid} OrderID={order.id}")
            receipts.append(
                SyncOperationReceipt(
                    operation_type="sale",
                    client_operation_uuid=uuid,
                    status="CONFIRMADA",
                    server_id=order.id,
                    rejection_reason=None,
                    processed_at=datetime.now(timezone.utc),
                )
            )

        except Exception as e:
            savepoint.rollback()
            # Manejo de condición de carrera / colisión concurrente de client_operation_uuid
            existing_concur_q = db.query(Order).filter(Order.client_operation_uuid == uuid)
            if tenant_id is not None:
                existing_concur_q = existing_concur_q.filter(Order.tenant_id == tenant_id)
            committed_order = existing_concur_q.first()
            if committed_order:
                receipts.append(
                    SyncOperationReceipt(
                        operation_type="sale",
                        client_operation_uuid=uuid,
                        status=committed_order.sync_status,
                        server_id=committed_order.id,
                        rejection_reason=committed_order.rejection_reason,
                        processed_at=datetime.now(timezone.utc),
                    )
                )
                if committed_order.sync_status == "CONFIRMADA":
                    confirmed_count += 1
                else:
                    conflict_count += 1
                continue

            conflict_count += 1
            error_msg = f"Error al procesar venta offline: {str(e)}"
            logger.error(f"[RNF10-SYNC][SALE_ERROR] UUID={uuid}: {error_msg}")
            receipts.append(
                SyncOperationReceipt(
                    operation_type="sale",
                    client_operation_uuid=uuid,
                    status="RECHAZADA_CONFLICTO",
                    server_id=None,
                    rejection_reason=error_msg,
                    processed_at=datetime.now(timezone.utc),
                )
            )

    # ─── B. Procesar Reservas Offline ────────────────────────────────────────
    for res_in in batch.reservations:
        uuid = res_in.client_operation_uuid

        # Idempotencia: Verificar si ya existe en este tenant
        existing_res_query = db.query(Reservation).filter(Reservation.client_operation_uuid == uuid)
        if tenant_id is not None:
            existing_res_query = existing_res_query.filter(Reservation.tenant_id == tenant_id)
        existing_res = existing_res_query.first()

        if existing_res:
            logger.info(
                f"[RNF10-SYNC][IDEMPOTENT_RES] UUID={uuid} ya procesada. ReservationID={existing_res.id}"
            )
            receipts.append(
                SyncOperationReceipt(
                    operation_type="reservation",
                    client_operation_uuid=uuid,
                    status=existing_res.sync_status,
                    server_id=existing_res.id,
                    rejection_reason=existing_res.rejection_reason,
                    processed_at=datetime.now(timezone.utc),
                )
            )
            if existing_res.sync_status == "CONFIRMADA":
                confirmed_count += 1
            else:
                conflict_count += 1
            continue

        # Validar pertenencia del cliente al tenant si se especifica user_id
        customer_id = current_user.id
        if res_in.user_id:
            cust_q = db.query(User).filter(User.id == res_in.user_id, User.is_active.is_(True))
            if tenant_id is not None:
                cust_q = cust_q.filter(User.tenant_id == tenant_id)
            cust = cust_q.first()
            if not cust:
                conflict_msg = f"Cliente con ID {res_in.user_id} no existe o no pertenece a la empresa {tenant_id}."
                logger.warning(f"[RNF10-SYNC][RES_CUST_CONFLICT] UUID={uuid}: {conflict_msg}")
                try:
                    conflict_res = Reservation(
                        tenant_id=tenant_id,
                        user_id=current_user.id,
                        branch_id=batch.branch_id,
                        status=ReservationStatus.cancelled,
                        pickup_date=res_in.pickup_date,
                        notes=res_in.notes,
                        client_operation_uuid=uuid,
                        is_offline_operation=True,
                        sync_status="RECHAZADA_CONFLICTO",
                        rejection_reason=conflict_msg,
                    )
                    db.add(conflict_res)
                    db.commit()
                except Exception:
                    db.rollback()

                conflict_count += 1
                receipts.append(
                    SyncOperationReceipt(
                        operation_type="reservation",
                        client_operation_uuid=uuid,
                        status="RECHAZADA_CONFLICTO",
                        server_id=None,
                        rejection_reason=conflict_msg,
                        processed_at=datetime.now(timezone.utc),
                    )
                )
                continue
            customer_id = cust.id

        savepoint = db.begin_nested()
        try:
            # 1. Bloqueo pesimista e inspección de stock para cada variante
            counts = quantities(res_in.items)
            conflict_detected = False
            conflict_msg = ""

            for vid, req_qty in counts.items():
                inv = (
                    db.query(Inventory)
                    .filter(Inventory.branch_id == batch.branch_id, Inventory.variant_id == vid)
                    .with_for_update()
                    .first()
                )
                available_stock = inv.stock if inv else 0
                if not inv or inv.stock < req_qty:
                    conflict_detected = True
                    conflict_msg = (
                        f"Stock insuficiente para reserva de variante {vid} en sucursal {batch.branch_id}. "
                        f"Solicitado: {req_qty}, Disponible central: {available_stock}."
                    )
                    break

            if conflict_detected:
                savepoint.rollback()
                conflict_count += 1
                logger.warning(f"[RNF10-SYNC][RES_CONFLICT] UUID={uuid}: {conflict_msg}")
                # Persistir conflicto para idempotencia
                try:
                    conflict_res = Reservation(
                        tenant_id=tenant_id,
                        user_id=customer_id,
                        branch_id=batch.branch_id,
                        status=ReservationStatus.cancelled,
                        pickup_date=res_in.pickup_date,
                        notes=res_in.notes,
                        client_operation_uuid=uuid,
                        is_offline_operation=True,
                        sync_status="RECHAZADA_CONFLICTO",
                        rejection_reason=conflict_msg,
                    )
                    db.add(conflict_res)
                    db.commit()
                except Exception:
                    db.rollback()

                receipts.append(
                    SyncOperationReceipt(
                        operation_type="reservation",
                        client_operation_uuid=uuid,
                        status="RECHAZADA_CONFLICTO",
                        server_id=None,
                        rejection_reason=conflict_msg,
                        processed_at=datetime.now(timezone.utc),
                    )
                )
                continue

            # 2. Descuento temporal / retención de stock
            reservation = Reservation(
                tenant_id=tenant_id,
                user_id=customer_id,
                branch_id=batch.branch_id,
                status=ReservationStatus.pending,
                pickup_date=res_in.pickup_date,
                notes=res_in.notes,
                client_operation_uuid=uuid,
                is_offline_operation=True,
                sync_status="CONFIRMADA",
            )
            db.add(reservation)
            db.flush()

            for vid, req_qty in counts.items():
                _, price = price_variant(db, vid)
                move_stock(
                    db=db,
                    branch_id=batch.branch_id,
                    variant_id=vid,
                    delta=-req_qty,
                    kind="offline_reservation",
                    user_id=current_user.id,
                    reference=f"sync_res:{uuid}",
                    tenant_id=tenant_id,
                )
                db.add(
                    ReservationItem(
                        tenant_id=tenant_id,
                        reservation_id=reservation.id,
                        variant_id=vid,
                        quantity=req_qty,
                        unit_price=float(price),
                    )
                )

            savepoint.commit()
            confirmed_count += 1
            logger.info(f"[RNF10-SYNC][RES_CONFIRMED] UUID={uuid} ReservationID={reservation.id}")
            receipts.append(
                SyncOperationReceipt(
                    operation_type="reservation",
                    client_operation_uuid=uuid,
                    status="CONFIRMADA",
                    server_id=reservation.id,
                    rejection_reason=None,
                    processed_at=datetime.now(timezone.utc),
                )
            )

        except Exception as e:
            savepoint.rollback()
            # Manejo de condición de carrera / colisión concurrente de client_operation_uuid
            existing_concur_q = db.query(Reservation).filter(Reservation.client_operation_uuid == uuid)
            if tenant_id is not None:
                existing_concur_q = existing_concur_q.filter(Reservation.tenant_id == tenant_id)
            committed_res = existing_concur_q.first()
            if committed_res:
                receipts.append(
                    SyncOperationReceipt(
                        operation_type="reservation",
                        client_operation_uuid=uuid,
                        status=committed_res.sync_status,
                        server_id=committed_res.id,
                        rejection_reason=committed_res.rejection_reason,
                        processed_at=datetime.now(timezone.utc),
                    )
                )
                if committed_res.sync_status == "CONFIRMADA":
                    confirmed_count += 1
                else:
                    conflict_count += 1
                continue

            conflict_count += 1
            error_msg = f"Error al procesar reserva offline: {str(e)}"
            logger.error(f"[RNF10-SYNC][RES_ERROR] UUID={uuid}: {error_msg}")
            receipts.append(
                SyncOperationReceipt(
                    operation_type="reservation",
                    client_operation_uuid=uuid,
                    status="RECHAZADA_CONFLICTO",
                    server_id=None,
                    rejection_reason=error_msg,
                    processed_at=datetime.now(timezone.utc),
                )
            )

    # Confirmar todas las operaciones válidas procesadas
    db.commit()

    logger.info(
        f"[RNF10-SYNC][BATCH_END] Total={len(receipts)} Confirmed={confirmed_count} Conflicts={conflict_count}"
    )

    return SyncBatchResponse(
        total_processed=len(receipts),
        confirmed_count=confirmed_count,
        conflict_count=conflict_count,
        receipts=receipts,
    )
