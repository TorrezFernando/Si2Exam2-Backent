"""Offline operations: immutable receipts, explicit price policy, per-operation savepoints."""
import hashlib
import json
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import text
from api.deps import get_user_permissions
from core.commerce import money, quantities, price_variant, move_stock
from db.models import Tenant, Branch, User, Inventory, Order, OrderItem, Reservation, ReservationItem
from db.models.sale import PaymentMethodEnum
from db.models.sync_operation import SyncOperation
from schemas.sync import SyncOperationReceipt, SyncBatchResponse


def context(db, user, branch_id):
    tenant = db.get(Tenant, user.tenant_id) if user.tenant_id else None
    if not tenant or not tenant.is_active:
        raise HTTPException(403, 'Se requiere una empresa activa y explícita.')
    branch = db.get(Branch, branch_id)
    if not branch or not branch.is_active or branch.tenant_id != tenant.id:
        raise HTTPException(403, 'La sucursal no pertenece a su empresa.')
    sub = tenant.active_subscription
    now = datetime.now(timezone.utc)
    end = sub.end_date if sub else None
    if end and end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if not sub or not sub.plan or not sub.plan.is_active or not sub.plan.has_offline_sync or (end and end <= now):
        raise HTTPException(403, 'La suscripción no habilita operación offline.')
    perms = get_user_permissions(db, user)
    # Consumers may reserve their own items at an active tenant branch.
    staff = bool(perms & {'sales:write', 'reservations:write', 'branches:write'})
    if staff and 'branches:write' not in perms and user.branch_id != branch.id:
        raise HTTPException(403, 'Se requiere la sucursal asignada o permiso multisucursal.')
    return tenant.id, perms


def process_batch(batch, db, user):
    tenant_id, perms = context(db, user, batch.branch_id)
    if batch.sales and 'sales:write' not in perms:
        raise HTTPException(403, 'Permiso sales:write requerido.')
    if any(r.user_id != user.id for r in batch.reservations) and 'reservations:write' not in perms:
        raise HTTPException(403, 'Solo puede reservar para sí mismo.')
    operations = [('sale', o) for o in batch.sales] + [('reservation', o) for o in batch.reservations]
    receipts = []
    try:
        # Lock in a deterministic order before taking any inventory locks. On PostgreSQL
        # the transaction lock also covers a UUID whose receipt does not exist yet.
        if db.bind.dialect.name == 'postgresql':
            keys = sorted({int.from_bytes(hashlib.sha256(f'{tenant_id}:{o.client_operation_uuid}'.encode()).digest()[:8], 'big', signed=True) for _, o in operations})
            for key in keys:
                db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})
        # Inventory locks are held until commit, so acquire across the complete batch.
        vids = sorted({i.variant_id for _, o in operations for i in o.items})
        if vids:
            db.query(Inventory).filter(Inventory.branch_id == batch.branch_id,
                Inventory.variant_id.in_(vids), Inventory.tenant_id == tenant_id).order_by(Inventory.variant_id).with_for_update().all()
        for kind, op in operations:
            uid = op.client_operation_uuid
            payload = {'type': kind, 'branch': batch.branch_id, 'payload': op.model_dump(mode='json')}
            fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            previous = db.query(SyncOperation).filter_by(tenant_id=tenant_id, client_operation_uuid=uid).first()
            if previous:
                if previous.owner_id != user.id or previous.fingerprint != fingerprint:
                    raise HTTPException(409, 'UUID reutilizado con otro contenido o propietario; conserve la operación original.')
                receipts.append(SyncOperationReceipt(**previous.receipt))
                continue
            model = Order if kind == 'sale' else Reservation
            legacy = db.query(model).filter_by(tenant_id=tenant_id, client_operation_uuid=uid).first()
            # Never execute pre-migration UUIDs again. A read-only reconciliation is
            # required if their original payload cannot be established reliably.
            if legacy:
                counts = quantities(op.items)
                if legacy.branch_id != batch.branch_id or (legacy.items and quantities(legacy.items) != counts):
                    raise HTTPException(409, 'UUID histórico incompatible: requiere conciliación.')
                receipt = SyncOperationReceipt(operation_type=kind, client_operation_uuid=uid,
                    status=legacy.sync_status, server_id=legacy.id,
                    rejection_reason=legacy.rejection_reason, processed_at=legacy.created_at or datetime.now(timezone.utc))
            else:
                try:
                    with db.begin_nested():
                        customer_id = op.user_id or user.id
                        customer = db.query(User).filter_by(id=customer_id, tenant_id=tenant_id, is_active=True).first()
                        if not customer:
                            raise HTTPException(409, 'Cliente no existe o no pertenece a la empresa.')
                        counts = quantities(op.items)
                        priced = []
                        for vid, qty in counts.items():
                            variant, price = price_variant(db, vid)
                            if variant.tenant_id != tenant_id or variant.product.tenant_id != tenant_id:
                                raise HTTPException(409, 'Variante no autorizada.')
                            priced.append((vid, qty, price))
                        if kind == 'sale':
                            if op.payment_method != PaymentMethodEnum.efectivo:
                                raise HTTPException(409, 'Solo se admite efectivo cobrado.')
                            prices = {vid: price for vid, _, price in priced}
                            total = sum((qty * price for _, qty, price in priced), money(0))
                            if any(money(i.unit_price) != prices[i.variant_id] for i in op.items) or money(op.total_amount) != total:
                                raise HTTPException(409, 'Precio o importe cobrado difiere del catálogo; requiere conciliación de caja.')
                            entity = Order(tenant_id=tenant_id, user_id=customer.id, branch_id=batch.branch_id,
                                order_type='presencial', payment_method='efectivo', payment_status='paid',
                                is_paid=True, payment_provider='Caja_Offline', total_amount=float(total),
                                client_operation_uuid=uid, is_offline_operation=True, sync_status='CONFIRMADA',
                                created_at=op.created_at or datetime.now(timezone.utc))
                        else:
                            if op.pickup_date and op.pickup_date.replace(tzinfo=op.pickup_date.tzinfo or timezone.utc) <= datetime.now(timezone.utc):
                                raise HTTPException(409, 'La fecha de retiro ya venció; requiere reprogramación.')
                            entity = Reservation(tenant_id=tenant_id, user_id=customer.id, branch_id=batch.branch_id,
                                status='pending', pickup_date=op.pickup_date, notes=op.notes,
                                client_operation_uuid=uid, is_offline_operation=True, sync_status='CONFIRMADA')
                        db.add(entity)
                        db.flush()
                        for vid, qty, price in priced:
                            move_stock(db, batch.branch_id, vid, -qty, 'offline_'+kind, user.id, uid, tenant_id)
                            item = dict(tenant_id=tenant_id, variant_id=vid, quantity=qty, unit_price=float(price))
                            db.add(OrderItem(order_id=entity.id, **item) if kind == 'sale' else ReservationItem(reservation_id=entity.id, **item))
                        db.flush()
                        receipt = SyncOperationReceipt(operation_type=kind, client_operation_uuid=uid,
                            status='CONFIRMADA', server_id=entity.id, rejection_reason=None, processed_at=datetime.now(timezone.utc))
                except HTTPException as error:
                    if error.status_code not in (400, 409):
                        raise
                    receipt = SyncOperationReceipt(operation_type=kind, client_operation_uuid=uid,
                        status='RECHAZADA_CONFLICTO', server_id=None, rejection_reason=str(error.detail),
                        processed_at=datetime.now(timezone.utc))
            db.add(SyncOperation(tenant_id=tenant_id, client_operation_uuid=uid, owner_id=user.id,
                branch_id=batch.branch_id, operation_type=kind, fingerprint=fingerprint, receipt=receipt.model_dump(mode='json')))
            db.flush()
            receipts.append(receipt)
        db.commit()
    except Exception:
        db.rollback()
        raise
    confirmed = sum(r.status == 'CONFIRMADA' for r in receipts)
    return SyncBatchResponse(total_processed=len(receipts), confirmed_count=confirmed,
        conflict_count=len(receipts)-confirmed, receipts=receipts)
