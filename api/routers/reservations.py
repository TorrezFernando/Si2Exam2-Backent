from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from api.deps import get_db, get_current_active_user, get_user_permissions, require_permission
from db.models.reservation import Reservation, ReservationItem, ReservationStatus
from db.models.branch import Branch
from schemas.reservation import Reservation as ReservationSchema, ReservationCreate, ReservationUpdate
from core.commerce import quantities, price_variant, move_stock, check_branch

router = APIRouter()

class ReservationStatusUpdate(BaseModel):
    status: ReservationStatus

def can_manage(db, user):
    return bool(get_user_permissions(db, user) & {'reservations:read', 'reservations:write', 'sales:write'})

def accessible(db, user, reservation_id, manage=False):
    query = db.query(Reservation).filter(Reservation.id == reservation_id)
    if user.tenant_id is not None:
        query = query.filter(Reservation.tenant_id == user.tenant_id)
    reservation = query.with_for_update().first()
    if not reservation:
        raise HTTPException(404, 'Reserva no encontrada')
    if manage or reservation.user_id != user.id:
        if not can_manage(db, user):
            raise HTTPException(403, 'No tienes acceso a esta reserva')
        check_branch(db, user, reservation.branch_id)
    return reservation

@router.get('', response_model=List[ReservationSchema])
def list_reservations(db: Session = Depends(get_db), skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100), branch_id: Optional[int] = None,
    status: Optional[ReservationStatus] = None, current_user=Depends(get_current_active_user)):
    query = db.query(Reservation)
    if current_user.tenant_id is not None:
        query = query.filter(Reservation.tenant_id == current_user.tenant_id)
    if can_manage(db, current_user):
        if 'branches:write' not in get_user_permissions(db, current_user):
            if not current_user.branch_id:
                raise HTTPException(403, 'No tienes sucursal asignada')
            query = query.filter(Reservation.branch_id == current_user.branch_id)
    else:
        query = query.filter(Reservation.user_id == current_user.id)
    if branch_id is not None:
        query = query.filter(Reservation.branch_id == branch_id)
    if status is not None:
        query = query.filter(Reservation.status == status)
    return query.order_by(Reservation.created_at.desc()).offset(skip).limit(limit).all()

@router.get('/{reservation_id}', response_model=ReservationSchema)
def get_reservation(reservation_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_active_user)):
    return accessible(db, current_user, reservation_id)

@router.post('', response_model=ReservationSchema, status_code=201)
def create_reservation(*, db: Session = Depends(get_db), reservation_in: ReservationCreate,
                       current_user=Depends(get_current_active_user)):
    try:
        branch = db.get(Branch, reservation_in.branch_id)
        if not branch or not branch.is_active:
            raise HTTPException(400, 'Sucursal no disponible')
        if current_user.tenant_id is not None and branch.tenant_id != current_user.tenant_id:
            raise HTTPException(403, 'La sucursal no pertenece a tu empresa')
        reservation = Reservation(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id, branch_id=branch.id,
            pickup_date=reservation_in.pickup_date, notes=reservation_in.notes, status=ReservationStatus.pending)
        db.add(reservation)
        db.flush()
        for variant_id, quantity in quantities(reservation_in.items).items():
            _, price = price_variant(db, variant_id)
            move_stock(db, branch.id, variant_id, -quantity, 'reservation', current_user.id, f'reservation:{reservation.id}')
            db.add(ReservationItem(
                tenant_id=current_user.tenant_id,
                reservation_id=reservation.id, variant_id=variant_id,
                quantity=quantity, unit_price=float(price)))

        db.commit()
        db.refresh(reservation)
        return reservation
    except Exception:
        db.rollback()
        raise

def transition(db, reservation, status, user):
    if status == reservation.status:
        return
    if reservation.status in (ReservationStatus.cancelled, ReservationStatus.completed):
        raise HTTPException(409, 'La reserva ya esta cerrada')
    if status == ReservationStatus.completed:
        raise HTTPException(409, 'Completa la reserva desde caja seleccionando las prendas compradas')
    if status == ReservationStatus.pending:
        raise HTTPException(409, 'No se puede volver a pendiente')
    if status == ReservationStatus.cancelled:
        for variant_id, qty in quantities(reservation.items).items():
            move_stock(db, reservation.branch_id, variant_id, qty, 'reservation_cancel', user.id, f'reservation:{reservation.id}')
    reservation.status = status

@router.patch('/{reservation_id}/cancel', response_model=ReservationSchema)
def cancel_reservation(reservation_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_active_user)):
    try:
        reservation = accessible(db, current_user, reservation_id)
        transition(db, reservation, ReservationStatus.cancelled, current_user)
        db.commit()
        db.refresh(reservation)
        return reservation
    except Exception:
        db.rollback()
        raise

@router.patch('/{reservation_id}/status', response_model=ReservationSchema)
def update_reservation_status(reservation_id: int, reservation_in: ReservationStatusUpdate,
    db: Session = Depends(get_db), current_user=Depends(require_permission('reservations:write', 'sales:write'))):
    return update_reservation(reservation_id, ReservationUpdate(status=reservation_in.status), db, current_user)

@router.patch('/{reservation_id}', response_model=ReservationSchema)
def update_reservation(reservation_id: int, reservation_in: ReservationUpdate,
    db: Session = Depends(get_db), current_user=Depends(require_permission('reservations:write', 'sales:write'))):
    try:
        reservation = accessible(db, current_user, reservation_id, manage=True)
        if reservation_in.status is not None:
            transition(db, reservation, reservation_in.status, current_user)
        for field, value in reservation_in.model_dump(exclude_unset=True).items():
            if field != 'status':
                setattr(reservation, field, value)
        db.commit()
        db.refresh(reservation)
        return reservation
    except Exception:
        db.rollback()
        raise
