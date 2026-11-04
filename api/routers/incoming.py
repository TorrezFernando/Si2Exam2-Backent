from datetime import date, datetime, timezone
import hashlib
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from api.deps import get_db, require_permission, get_user_permissions
from core.access import company_id
from core.commerce import check_branch, move_stock, price_variant
from db.models.incoming import IncomingStock, IncomingReceipt
from db.models.operations import Supplier
from db.models.branch import Branch, Inventory

router = APIRouter()


class IncomingInput(BaseModel):
    branch_id: int = Field(gt=0)
    supplier_id: int = Field(gt=0)
    variant_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=10000)
    expected_date: date
    reference: str = Field(min_length=3, max_length=200)
    request_key: str = Field(min_length=10, max_length=64)


class ReceiptInput(BaseModel):
    quantity: int = Field(gt=0, le=10000)
    request_key: str = Field(min_length=10, max_length=64)


def scoped(db, user):
    query = db.query(IncomingStock).filter_by(tenant_id=company_id(user))
    if 'branches:write' not in get_user_permissions(db, user):
        check_branch(db, user, user.branch_id)
        query = query.filter_by(branch_id=user.branch_id)
    return query


def present(row):
    return {**{c.name: getattr(row, c.name) for c in IncomingStock.__table__.columns if not c.name.startswith('request_')},
            'pending_quantity': row.quantity-row.received_quantity if row.status != 'cancelled' else 0}


@router.get('')
def list_incoming(db: Session = Depends(get_db), user=Depends(require_permission('inventory:read', 'inventory:write')),
                  skip: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    return [present(r) for r in scoped(db, user).order_by(IncomingStock.expected_date, IncomingStock.id).offset(skip).limit(limit)]


@router.post('', status_code=201)
def create_incoming(data: IncomingInput, db: Session = Depends(get_db), user=Depends(require_permission('inventory:write'))):
    try:
        tenant = company_id(user)
        check_branch(db, user, data.branch_id)
        fingerprint = hashlib.sha256(data.model_dump_json(exclude={'request_key'}).encode()).hexdigest()
        previous = db.query(IncomingStock).filter_by(tenant_id=tenant, request_key=data.request_key).first()
        if previous:
            if previous.request_fingerprint != fingerprint:
                raise HTTPException(409, 'La clave ya corresponde a otra previsión.')
            return present(previous)
        supplier = db.query(Supplier).filter_by(id=data.supplier_id, tenant_id=tenant, is_active=True).first()
        if not supplier:
            raise HTTPException(404, 'Proveedor no disponible para esta empresa.')
        price_variant(db, data.variant_id, tenant)
        if data.expected_date < datetime.now(timezone.utc).date():
            raise HTTPException(400, 'La fecha prevista no puede ser anterior a hoy (UTC).')
        row = IncomingStock(**data.model_dump(), tenant_id=tenant, request_fingerprint=fingerprint)
        db.add(row); db.commit(); db.refresh(row)
        return present(row)
    except IntegrityError:
        db.rollback()
        raise HTTPException(503, 'Solicitud concurrente: reintenta con la misma clave.')
    except Exception:
        db.rollback()
        raise


@router.post('/{incoming_id}/receive')
def receive(incoming_id: int, data: ReceiptInput, db: Session = Depends(get_db), user=Depends(require_permission('inventory:write'))):
    try:
        row = scoped(db, user).filter_by(id=incoming_id).with_for_update().first()
        if not row:
            raise HTTPException(404, 'Previsión no encontrada.')
        check_branch(db, user, row.branch_id)
        previous = db.query(IncomingReceipt).filter_by(tenant_id=row.tenant_id, request_key=data.request_key).first()
        if previous:
            if previous.incoming_id != row.id or previous.quantity != data.quantity:
                raise HTTPException(409, 'La clave ya corresponde a otra recepción.')
            return {'receipt_id': previous.id, 'incoming': present(row)}
        if row.status not in ('pending', 'partial') or data.quantity > row.quantity-row.received_quantity:
            raise HTTPException(409, 'Recepción cerrada o cantidad superior a la pendiente.')
        price_variant(db, row.variant_id, row.tenant_id)
        db.query(Branch).filter_by(id=row.branch_id).with_for_update().first()
        if not db.query(Inventory).filter_by(tenant_id=row.tenant_id, branch_id=row.branch_id, variant_id=row.variant_id).first():
            db.add(Inventory(tenant_id=row.tenant_id, branch_id=row.branch_id, variant_id=row.variant_id, stock=0)); db.flush()
        move_stock(db, row.branch_id, row.variant_id, data.quantity, 'scheduled_receipt', user.id, f'incoming:{row.id}:{data.request_key}')
        row.received_quantity += data.quantity
        row.status = 'received' if row.received_quantity == row.quantity else 'partial'
        receipt = IncomingReceipt(tenant_id=row.tenant_id, incoming_id=row.id, user_id=user.id, **data.model_dump())
        db.add(receipt); db.commit(); db.refresh(receipt)
        return {'receipt_id': receipt.id, 'incoming': present(row)}
    except IntegrityError:
        db.rollback()
        raise HTTPException(503, 'Solicitud concurrente: reintenta la misma recepción.')
    except Exception:
        db.rollback()
        raise


@router.post('/{incoming_id}/cancel')
def cancel(incoming_id: int, db: Session = Depends(get_db), user=Depends(require_permission('inventory:write'))):
    row = scoped(db, user).filter_by(id=incoming_id).with_for_update().first()
    if not row:
        raise HTTPException(404, 'Previsión no encontrada.')
    if row.status == 'received':
        raise HTTPException(409, 'La recepción completa no se puede cancelar.')
    row.status = 'cancelled'
    db.commit()
    return present(row)
