from typing import Any, List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from api.deps import (
    get_db,
    require_permission,
    get_current_user_optional,
    get_current_active_user,
    verify_branch_limit,
)
from db.models.branch import Branch
from schemas.branch import Branch as BranchSchema, BranchCreate, BranchUpdate

router = APIRouter()

@router.get("", response_model=List[BranchSchema])
def list_branches(
    db: Session = Depends(get_db),
    active_only: bool = True,
    current_user = Depends(get_current_user_optional),
) -> Any:
    """Lista sucursales filtrando automáticamente por el tenant del usuario si está autenticado."""
    query = db.query(Branch)
    if current_user and current_user.tenant_id is not None:
        query = query.filter(Branch.tenant_id == current_user.tenant_id)
    if active_only:
        query = query.filter(Branch.is_active == True)
    return query.all()

@router.get("/{branch_id}", response_model=BranchSchema)
def get_branch(
    branch_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user_optional),
) -> Any:
    query = db.query(Branch).filter(Branch.id == branch_id)
    if current_user and current_user.tenant_id is not None:
        query = query.filter(Branch.tenant_id == current_user.tenant_id)
    branch = query.first()
    if not branch:
        raise HTTPException(status_code=404, detail="Sucursal no encontrada")
    return branch

@router.post("", response_model=BranchSchema, status_code=status.HTTP_201_CREATED)
def create_branch(
    *,
    db: Session = Depends(get_db),
    branch_in: BranchCreate,
    current_user = Depends(require_permission("branches:write")),
    _limit_check = Depends(verify_branch_limit),
) -> Any:
    """
    Crea una nueva sucursal (Solo Admin).
    Verifica que no exceda el límite del plan de suscripción del Tenant.
    """
    branch = Branch(
        tenant_id=current_user.tenant_id,
        name=branch_in.name,
        city=branch_in.city,
        address=branch_in.address,
        phone=branch_in.phone,
        is_active=branch_in.is_active
    )
    db.add(branch)
    db.commit()
    db.refresh(branch)
    return branch

@router.patch("/{branch_id}", response_model=BranchSchema)
def update_branch(
    *,
    db: Session = Depends(get_db),
    branch_id: int,
    branch_in: BranchUpdate,
    current_user = Depends(require_permission("branches:write")),
) -> Any:
    """Actualiza una sucursal (Solo Admin con acceso a su tenant)."""
    query = db.query(Branch).filter(Branch.id == branch_id)
    if current_user.tenant_id is not None:
        query = query.filter(Branch.tenant_id == current_user.tenant_id)
    branch = query.first()
    if not branch:
        raise HTTPException(status_code=404, detail="Sucursal no encontrada")
        
    update_data = branch_in.model_dump(exclude_unset=True)
    # Prohibir modificar tenant_id
    update_data.pop("tenant_id", None)
    for field, value in update_data.items():
        setattr(branch, field, value)
        
    db.commit()
    db.refresh(branch)
    return branch

@router.delete("/{branch_id}", response_model=BranchSchema)
def delete_branch(
    *,
    db: Session = Depends(get_db),
    branch_id: int,
    current_user = Depends(require_permission("branches:write")),
) -> Any:
    """Desactiva una sucursal (soft delete) (Solo Admin)."""
    query = db.query(Branch).filter(Branch.id == branch_id)
    if current_user.tenant_id is not None:
        query = query.filter(Branch.tenant_id == current_user.tenant_id)
    branch = query.first()
    if not branch:
        raise HTTPException(status_code=404, detail="Sucursal no encontrada")
        
    branch.is_active = False
    db.commit()
    db.refresh(branch)
    return branch

