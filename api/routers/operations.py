from typing import Optional, Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from api.deps import get_db, require_permission, get_user_permissions
from core.commerce import check_branch, move_stock
from db.models.operations import Supplier, Season, Collection, InventoryMovement
from db.models.product import Category, ProductVariant
from db.models.branch import Inventory

router = APIRouter()
MODELS = {'suppliers': Supplier, 'seasons': Season, 'collections': Collection, 'categories': Category}


class MasterData(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    contact: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)


def model_for(kind):
    if kind not in MODELS:
        raise HTTPException(404, 'Catalogo no encontrado')
    return MODELS[kind]


@router.get('/masters/{kind}')
def list_master(kind: str, db: Session = Depends(get_db), user=Depends(require_permission('catalog:write', 'inventory:write'))):
    model = model_for(kind)
    return db.query(model).order_by(model.name).all()


@router.post('/masters/{kind}', status_code=201)
def create_master(kind: str, data: MasterData, db: Session = Depends(get_db), user=Depends(require_permission('catalog:write'))):
    model = model_for(kind)
    obj = model(**{k: v for k, v in data.model_dump().items() if hasattr(model, k)})
    try:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'Ya existe un registro con ese nombre')


@router.patch('/masters/{kind}/{record_id}')
def update_master(kind: str, record_id: int, data: MasterData, db: Session = Depends(get_db), user=Depends(require_permission('catalog:write'))):
    model = model_for(kind)
    obj = db.get(model, record_id)
    if not obj:
        raise HTTPException(404, 'Registro no encontrado')
    for key, value in data.model_dump(exclude_unset=True).items():
        if hasattr(model, key):
            setattr(obj, key, value)
    try:
        db.commit()
        db.refresh(obj)
        return obj
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'Ya existe un registro con ese nombre')


@router.get('/movements')
def list_movements(db: Session = Depends(get_db), branch_id: Optional[int] = None,
    limit: int = Query(100, ge=1, le=500), user=Depends(require_permission('inventory:read', 'inventory:write'))):
    query = db.query(InventoryMovement)
    if 'branches:write' not in get_user_permissions(db, user):
        check_branch(db, user, user.branch_id)
        branch_id = user.branch_id
    if branch_id is not None:
        query = query.filter(InventoryMovement.branch_id == branch_id)
    return query.order_by(InventoryMovement.id.desc()).limit(limit).all()


class MovementInput(BaseModel):
    branch_id: int = Field(gt=0)
    variant_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=10000)
    kind: Literal['receipt', 'adjustment_in', 'adjustment_out']
    reference: str = Field(min_length=3, max_length=200)


@router.post('/movements', status_code=201)
def create_movement(data: MovementInput, db: Session = Depends(get_db), user=Depends(require_permission('inventory:write'))):
    try:
        # Branch lock serializes initial inventory creation as well.
        from db.models.branch import Branch
        db.query(Branch).filter(Branch.id == data.branch_id).with_for_update().first()
        check_branch(db, user, data.branch_id)
        if not db.get(ProductVariant, data.variant_id):
            raise HTTPException(404, 'Variante no encontrada')
        inventory = db.query(Inventory).filter_by(branch_id=data.branch_id, variant_id=data.variant_id).first()
        if not inventory:
            db.add(Inventory(branch_id=data.branch_id, variant_id=data.variant_id, stock=0))
            db.flush()
        delta = -data.quantity if data.kind == 'adjustment_out' else data.quantity
        move_stock(db, data.branch_id, data.variant_id, delta, data.kind, user.id, data.reference)
        db.commit()
        return {'ok': True}
    except Exception:
        db.rollback()
        raise
