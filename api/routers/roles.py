from typing import Any, List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload

from api.deps import get_db, require_permission, require_superadmin
from api.audit_logger import log_audit
from db.models.rbac import Role, Permission, RolePermission
from schemas.rbac import RoleSchema, RoleCreate, RoleUpdate, PermissionSchema
from db.models.user import User
from api.deps import get_user_permissions
from core.access import is_global_admin, company_id

router = APIRouter()


@router.get("/permissions", response_model=List[PermissionSchema])
def list_permissions(
    db: Session = Depends(get_db),
    current_user = Depends(require_permission("roles:manage"))
) -> Any:
    query = db.query(Permission)
    if not is_global_admin(current_user):
        company_id(current_user)
        query = query.filter(Permission.name.in_(get_user_permissions(db, current_user)))
    return query.all()


@router.get("", response_model=List[RoleSchema])
def list_roles(
    db: Session = Depends(get_db),
    current_user = Depends(require_permission("roles:manage"))
) -> Any:
    roles = db.query(Role).options(joinedload(Role.permissions)).all()
    if is_global_admin(current_user):
        return roles
    company_id(current_user)
    allowed = get_user_permissions(db, current_user)
    return [role for role in roles if role.name != 'superadmin'
            and {p.name for p in role.permissions}.issubset(allowed)]


@router.post("", response_model=RoleSchema, status_code=status.HTTP_201_CREATED)
def create_role(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_superadmin),
    role_in: RoleCreate
) -> Any:
    existing = db.query(Role).filter(Role.name == role_in.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Ya existe un rol con ese nombre.")

    role = Role(name=role_in.name, description=role_in.description)
    db.add(role)
    db.commit()
    db.refresh(role)

    # Assign permissions
    for p_id in role_in.permission_ids:
        perm = db.query(Permission).filter(Permission.id == p_id).first()
        if perm:
            db.add(RolePermission(role_id=role.id, permission_id=perm.id))
    db.commit()

    log_audit(db, current_user.id, "CREATE", "ROLE", str(role.id), {"name": role.name})

    return db.query(Role).options(joinedload(Role.permissions)).filter(Role.id == role.id).first()


@router.patch("/{role_id}", response_model=RoleSchema)
def update_role(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_superadmin),
    role_id: int,
    role_in: RoleUpdate
) -> Any:
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Rol no encontrado.")

    if role.is_predefined:
        # Prevent editing name of predefined roles. Allowed to edit description and permissions (except admin)
        if role_in.name and role_in.name != role.name:
            raise HTTPException(status_code=400, detail="No puedes cambiar el nombre de un rol predeterminado.")
            
    if role.name == "admin":
        if role_in.permission_ids is not None:
            raise HTTPException(status_code=400, detail="El rol Admin es intocable e inmodificable.")

    update_data = role_in.model_dump(exclude_unset=True)
    if "name" in update_data: role.name = update_data["name"]
    if "description" in update_data: role.description = update_data["description"]
    
    if "permission_ids" in update_data and role.name != "admin":
        # Delete old
        db.query(RolePermission).filter(RolePermission.role_id == role.id).delete()
        # Add new
        for p_id in update_data["permission_ids"]:
            perm = db.query(Permission).filter(Permission.id == p_id).first()
            if perm:
                db.add(RolePermission(role_id=role.id, permission_id=perm.id))

    db.commit()

    log_audit(db, current_user.id, "UPDATE", "ROLE", str(role.id), update_data)

    return db.query(Role).options(joinedload(Role.permissions)).filter(Role.id == role.id).first()


@router.delete("/{role_id}")
def delete_role(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_superadmin),
    role_id: int
) -> Any:
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Rol no encontrado.")

    if role.is_predefined:
        raise HTTPException(status_code=400, detail="No puedes eliminar un rol predeterminado.")

    # Reasignar usuarios a cliente (u otro fallback) antes de borrar? 
    # Mejor prohibir si hay usuarios
    if db.query(User).filter(User.role_id == role.id).count() > 0:
        raise HTTPException(status_code=400, detail="No puedes eliminar un rol que tiene usuarios asignados.")

    db.delete(role)
    db.commit()

    log_audit(db, current_user.id, "DELETE", "ROLE", str(role_id), {"name": role.name})

    return {"message": "Rol eliminado exitosamente"}
