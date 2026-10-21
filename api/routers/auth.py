from datetime import timedelta
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session, joinedload

from api import deps
from api.audit_logger import log_audit
from core import security
from core.config import settings
from db.models.user import User
from db.models.rbac import Role
from schemas.user import User as UserSchema, UserCreate, Token

router = APIRouter()


@router.post("/login", response_model=Token)
def login_access_token(
    db: Session = Depends(deps.get_db),
    form_data: OAuth2PasswordRequestForm = Depends(),
) -> Any:
    user = (
        db.query(User)
        .options(joinedload(User.role), joinedload(User.tenant))
        .filter(User.email == form_data.username)
        .first()
    )
    if not user or not security.verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email o contraseña incorrectos",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Usuario inactivo. Contacta al administrador.",
        )

    role_name = user.role.name if user.role else None

    # Resolver Tenant y Plan de Suscripción activo
    tenant_id = user.tenant_id
    plan_code = "enterprise_superadmin" if role_name == "superadmin" else "basic"
    plan_id = None
    if user.tenant:
        if not user.tenant.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="La empresa o tenant asociado a esta cuenta se encuentra inactivo.",
            )
        active_sub = user.tenant.active_subscription
        if active_sub and active_sub.plan:
            plan_code = active_sub.plan.code
            plan_id = active_sub.plan.id

    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = security.create_access_token(
        subject=user.id,
        role=role_name,
        expires_delta=access_token_expires,
        tenant_id=tenant_id,
        plan_code=plan_code,
        plan_id=plan_id,
    )
    
    log_audit(db, user.id, "LOGIN", "AUTH", str(user.id), None, tenant_id=tenant_id)
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "role": role_name,
        "tenant_id": tenant_id,
        "plan_code": plan_code,
    }


@router.post("/register", response_model=UserSchema, status_code=status.HTTP_201_CREATED)
def register_user(
    *,
    db: Session = Depends(deps.get_db),
    user_in: UserCreate,
) -> Any:
    existing = db.query(User).filter(User.email == user_in.email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ya existe una cuenta con ese correo electrónico.",
        )
        
    cliente_role = db.query(Role).filter(Role.name == "cliente").first()
    if not cliente_role:
        raise HTTPException(status_code=500, detail="Rol 'cliente' no configurado en BD.")

    tenant_id = user_in.tenant_id
    if not tenant_id:
        from db.models.tenant import Tenant
        default_tenant = db.query(Tenant).filter(Tenant.is_active == True).first()
        if default_tenant:
            tenant_id = default_tenant.id
        
    user = User(
        email=user_in.email,
        hashed_password=security.get_password_hash(user_in.password),
        full_name=user_in.full_name,
        phone=user_in.phone,
        tenant_id=tenant_id,
        role_id=cliente_role.id
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    
    log_audit(db, user.id, "REGISTER", "AUTH", str(user.id), None, tenant_id=tenant_id)
    
    return user



@router.get("/me", response_model=UserSchema)
def read_users_me(
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    return current_user
