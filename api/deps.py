from typing import Generator, Optional
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from pydantic import ValidationError
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload

from core.config import settings
from db.database import SessionLocal
from db.models.user import User
from db.models.tenant import Tenant, SubscriptionPlan, Subscription
from db.models.branch import Branch
from db.models.rbac import Role, Permission, UserPermission
from schemas.user import TokenPayload

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_STR}/auth/login"
)


def get_db() -> Generator:
    """Dependency que provee una sesión de BD por request."""
    try:
        db = SessionLocal()
        yield db
    finally:
        db.close()


def get_current_user(
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
) -> User:
    """
    Valida el JWT y devuelve el usuario autenticado (con su rol y tenant cargados).
    Lanza 401 si el token es inválido o expirado.
    """
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        token_data = TokenPayload(**payload)
        user_id = int(token_data.sub)
    except (JWTError, ValidationError, ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
            detail="No se pudo validar las credenciales. Token inválido o expirado.",
        )
    # Cargar usuario con su rol y tenant asociado usando joinedload para evitar N+1
    user = (
        db.query(User)
        .options(joinedload(User.role), joinedload(User.tenant))
        .filter(User.id == user_id)
        .first()
    )
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")
    return user



def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Valida que el usuario esté activo."""
    if not current_user.is_active:
        raise HTTPException(status_code=401, detail="Usuario inactivo.")
    return current_user

oauth2_scheme_optional = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_STR}/auth/login", 
    auto_error=False
)

def get_current_user_optional(
    db: Session = Depends(get_db),
    token: Optional[str] = Depends(oauth2_scheme_optional),
) -> User | None:
    if not token:
        return None
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        token_data = TokenPayload(**payload)
        user_id = int(token_data.sub)
        user_id = int(token_data.sub)
    except (JWTError, ValidationError, ValueError, TypeError):
        return None
    user = db.query(User).options(joinedload(User.role)).filter(User.id == user_id).first()
    return user

def get_user_permissions(db: Session, user: User) -> set[str]:
    """Obtiene el set de permisos (strings) de un usuario (Rol + UserPermissions)."""
    permissions = set()
    
    # 1. Permisos del rol
    if user.role:
        for perm in user.role.permissions:
            permissions.add(perm.name)
            
    # 2. Permisos específicos del usuario (override)
    user_perms = db.query(UserPermission).options(joinedload(UserPermission.permission)).filter(UserPermission.user_id == user.id).all()
    for up in user_perms:
        if up.is_granted:
            permissions.add(up.permission.name)
        else:
            if up.permission.name in permissions:
                permissions.remove(up.permission.name)
                
    return permissions

# ─── Dependencies de Autorización por Permiso (RBAC) ──────────────────────────

def require_permission(*required_perms: str):
    """
    Factory de dependency que restringe el acceso a usuarios con un permiso específico.
    Si se pasan varios, el usuario debe tener AL MENOS UNO de ellos (OR).
    """
    def permission_checker(
        current_user: User = Depends(get_current_active_user),
        db: Session = Depends(get_db)
    ) -> User:
        # Superadmin siempre tiene acceso irrestricto
        if current_user.role and current_user.role.name == "superadmin":
            return current_user

        user_perms = get_user_permissions(db, current_user)
        has_access = any(req_perm in user_perms for req_perm in required_perms)
        
        if not has_access:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Acceso denegado. Se requiere alguno de estos permisos: {required_perms}",
            )
        return current_user
    return permission_checker


# Shortcuts comunes basados en permisos
require_admin_role = require_permission("roles:manage") # Solo admins manejan roles
require_staff = require_permission("inventory:read", "sales:write")


def require_superadmin(
    current_user: User = Depends(get_current_active_user),
) -> User:
    """Restringe el acceso exclusivamente al Superadministrador Global SaaS."""
    if not current_user.role or current_user.role.name != "superadmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso restringido únicamente a Superadministradores de la plataforma SaaS.",
        )
    return current_user


# ─── Dependencies de Multitenancy y Guardias de Suscripción (RNF01, RNF02) ────

def get_current_tenant_id(
    request: Request,
    current_user: User = Depends(get_current_active_user),
) -> int:
    """
    RNF01 / RNF02: Obtiene el tenant_id garantizado desde el usuario autenticado (via JWT).
    Si el usuario es superadmin y envía header 'x-tenant-id', actúa en contexto de esa empresa.
    """
    if current_user.tenant_id is not None:
        return current_user.tenant_id

    if current_user.role and current_user.role.name == "superadmin":
        header_tenant = request.headers.get("x-tenant-id")
        if header_tenant:
            try:
                return int(header_tenant)
            except ValueError:
                pass
        return 1

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Usuario sin tenant / empresa asignada.",
    )


def get_current_tenant(
    request: Request,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Tenant:
    """
    Obtiene la instancia de Tenant asociada al usuario autenticado.
    Verifica que la empresa esté activa.
    """
    tenant_id = get_current_tenant_id(request, current_user)
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant or not tenant.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="La empresa o tenant asignado no existe o se encuentra inactivo.",
        )
    return tenant


def verify_branch_limit(
    request: Request,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Guardia de suscripción: Bloquea la creación de más sucursales activas
    de las permitidas por el plan contratado (ej. máx 2 en Básico, máx 10 en Profesional).
    """
    if current_user.role and current_user.role.name == "superadmin" and not request.headers.get("x-tenant-id"):
        return

    tenant = get_current_tenant(request, current_user, db)
    sub = tenant.active_subscription
    if not sub or not sub.plan:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No se encontró un plan de suscripción activo para esta empresa.",
        )
    
    current_count = db.query(Branch).filter(
        Branch.tenant_id == tenant.id,
        Branch.is_active == True,
    ).count()

    if current_count >= sub.plan.max_branches:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Límite de sucursales alcanzado ({sub.plan.max_branches}) "
                f"para el {sub.plan.name}. Actualiza al Plan Profesional para habilitar hasta 10 sucursales."
            ),
        )


def require_feature(feature_name: str):
    """
    Guardia de suscripción: Valida que el plan activo del tenant habilite
    una funcionalidad específica (ej. 'ai_reports').
    Devuelve HTTP 403 claro si el plan no lo permite.
    """
    def feature_checker(
        request: Request,
        current_user: User = Depends(get_current_active_user),
        db: Session = Depends(get_db),
    ) -> User:
        if current_user.role and current_user.role.name == "superadmin":
            return current_user

        tenant = get_current_tenant(request, current_user, db)
        sub = tenant.active_subscription
        if not sub or not sub.plan:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No se encontró una suscripción activa para la empresa.",
            )
        
        if feature_name == "ai_reports" and not sub.plan.has_ai_reports:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"La funcionalidad '{feature_name}' no está disponible en el {sub.plan.name}. "
                    "Actualiza al Plan Profesional para acceder a reportes generativos con IA."
                ),
            )
        return current_user
    return feature_checker

