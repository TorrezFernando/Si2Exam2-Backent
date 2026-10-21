from typing import Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session, joinedload

from api.deps import (
    get_db,
    get_current_active_user,
    require_permission,
)
from api.audit_logger import log_audit
from core import security
from db.models.user import User
from db.models.rbac import Role, UserPermission
from db.models.branch import Branch
from schemas.user import (
    User as UserSchema,
    UserCreateByAdmin,
    UserUpdate,
    PasswordChange,
)

router = APIRouter()


# ─── LISTAR USUARIOS ──────────────────────────────────────────────────────────

@router.get("", response_model=List[UserSchema])
def list_users(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("users:read")),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    role_id: Optional[int] = None,
    branch_id: Optional[int] = None,
    is_active: Optional[bool] = None,
) -> Any:
    query = db.query(User).options(joinedload(User.role), joinedload(User.user_permissions))
    if current_user.tenant_id is not None:
        query = query.filter(User.tenant_id == current_user.tenant_id)

    if role_id is not None:
        query = query.filter(User.role_id == role_id)
    if branch_id is not None:
        query = query.filter(User.branch_id == branch_id)
    if is_active is not None:
        query = query.filter(User.is_active == is_active)

    return query.offset(skip).limit(limit).all()


# ─── CREAR USUARIO ──────────────────────────────────────────────────────────

@router.post("", response_model=UserSchema, status_code=status.HTTP_201_CREATED)
def create_user_by_admin(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("users:write")),
    user_in: UserCreateByAdmin,
) -> Any:
    existing = db.query(User).filter(User.email == user_in.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Ya existe un usuario con este correo.")

    role = db.query(Role).filter(Role.id == user_in.role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Rol no encontrado.")

    # Logica simple para obligar branch a encargados/cajeros
    if role.name in ("cajero", "encargado") and not user_in.branch_id:
        raise HTTPException(status_code=422, detail="Roles operativos requieren branch_id.")

    if user_in.branch_id:
        branch = db.query(Branch).filter(Branch.id == user_in.branch_id).first()
        if not branch or (current_user.tenant_id and branch.tenant_id != current_user.tenant_id):
            raise HTTPException(status_code=400, detail="Sucursal no válida o no pertenece a tu empresa.")

    new_user = User(
        email=user_in.email,
        hashed_password=security.get_password_hash(user_in.password),
        full_name=user_in.full_name,
        phone=user_in.phone,
        tenant_id=current_user.tenant_id,
        role_id=user_in.role_id,
        branch_id=user_in.branch_id,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    log_audit(db, current_user.id, "CREATE", "USER", str(new_user.id), {"email": new_user.email}, tenant_id=current_user.tenant_id)

    return new_user


# ─── VER USUARIO POR ID ───────────────────────────────────────────────────────

@router.get("/{user_id}", response_model=UserSchema)
def get_user(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("users:read")),
    user_id: int,
) -> Any:
    query = db.query(User).options(joinedload(User.role), joinedload(User.user_permissions)).filter(User.id == user_id)
    if current_user.tenant_id is not None:
        query = query.filter(User.tenant_id == current_user.tenant_id)
    user = query.first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")
    return user


# ─── ACTUALIZAR USUARIO ───────────────────────────────────────────────────────

@router.patch("/{user_id}", response_model=UserSchema)
def update_user(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("users:write")),
    user_id: int,
    user_in: UserUpdate,
) -> Any:
    query = db.query(User).filter(User.id == user_id)
    if current_user.tenant_id is not None:
        query = query.filter(User.tenant_id == current_user.tenant_id)
    user = query.first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")

    # Protect Admin role logic
    if user.role and user.role.name == "admin" and user_in.role_id is not None and user_in.role_id != user.role_id:
        admin_count = db.query(User).join(Role).filter(Role.name == "admin", User.is_active == True).count()
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="No puedes quitarle el rol al último administrador.")

    update_data = user_in.model_dump(exclude_unset=True)
    update_data.pop("tenant_id", None)
    old_values = {k: getattr(user, k) for k in update_data.keys()}

    for field, value in update_data.items():
        setattr(user, field, value)

    db.commit()
    db.refresh(user)

    log_audit(db, current_user.id, "UPDATE", "USER", str(user.id), {"old": old_values, "new": update_data}, tenant_id=current_user.tenant_id)

    # Recargar con relaciones
    return db.query(User).options(joinedload(User.role)).filter(User.id == user_id).first()



# ─── GESTIONAR PERMISOS INDIVIDUALES DE USUARIO ──────────────────────────────

@router.post("/{user_id}/permissions")
def update_user_permissions(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("roles:manage")),
    user_id: int,
    permission_id: int,
    is_granted: bool
) -> Any:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")
        
    # Check if admin is trying to be modified (Admin has all perms by default)
    if user.role and user.role.name == "admin":
        raise HTTPException(status_code=400, detail="El administrador principal es inmodificable en sus permisos.")

    up = db.query(UserPermission).filter(
        UserPermission.user_id == user_id, 
        UserPermission.permission_id == permission_id
    ).first()

    if up:
        up.is_granted = is_granted
    else:
        up = UserPermission(user_id=user_id, permission_id=permission_id, is_granted=is_granted)
        db.add(up)

    db.commit()
    
    log_audit(db, current_user.id, "UPDATE", "USER_PERMISSION", str(user_id), {"permission_id": permission_id, "is_granted": is_granted})
    
    return {"message": "Permiso actualizado correctamente"}


# ─── DESACTIVAR USUARIO ────────────────────────────────────────────────────────

@router.delete("/{user_id}", response_model=UserSchema)
def deactivate_user(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("users:write")),
    user_id: int,
) -> Any:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")

    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="No puedes desactivarte a ti mismo.")

    if user.role and user.role.name == "admin":
        admin_count = db.query(User).join(Role).filter(Role.name == "admin", User.is_active == True).count()
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="No se puede desactivar al último administrador.")

    user.is_active = False
    db.commit()
    
    log_audit(db, current_user.id, "DELETE", "USER", str(user.id), {"is_active": False})
    
    return user


# ─── CAMBIAR CONTRASEÑA PROPIA ────────────────────────────────────────────────

@router.post("/change-password", status_code=status.HTTP_200_OK)
def change_own_password(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    password_data: PasswordChange,
) -> Any:
    if not security.verify_password(password_data.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="La contraseña actual es incorrecta.")

    current_user.hashed_password = security.get_password_hash(password_data.new_password)
    db.commit()
    
    log_audit(db, current_user.id, "UPDATE", "USER_PASSWORD", str(current_user.id), None)
    
    return {"message": "Contraseña actualizada exitosamente."}
