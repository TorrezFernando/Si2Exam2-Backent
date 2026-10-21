from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional, List
from datetime import datetime
import re
from schemas.rbac import RoleSchema, UserPermissionSchema

# ─── Schemas base ─────────────────────────────────────────────────────────────

class UserBase(BaseModel):
    email: EmailStr
    full_name: Optional[str] = None
    phone: Optional[str] = None
    tenant_id: Optional[int] = None



# ─── Registro público (CU-01) ─────────────────────────────────────────────────

class UserCreate(UserBase):
    """Schema para el registro público de un nuevo cliente."""
    password: str

    @field_validator('password')
    @classmethod
    def validate_password(cls, v):
        if len(v) < 6:
            raise ValueError("La contraseña debe tener al menos 6 caracteres")
        if not re.search(r"[A-Z]", v):
            raise ValueError("La contraseña debe contener al menos una mayúscula")
        if not re.search(r"\d", v):
            raise ValueError("La contraseña debe contener al menos un número")
        if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", v):
            raise ValueError("La contraseña debe contener al menos un carácter especial")
        return v


# ─── Creación de usuario por Admin (CU-03) ────────────────────────────────────

class UserCreateByAdmin(UserBase):
    """
    Schema que usa el Admin para crear cualquier tipo de usuario (empleados).
    Permite especificar rol y la sucursal a la que pertenece (para cajero/encargado).
    """
    password: str
    role_id: int
    branch_id: Optional[int] = None  # Obligatorio si role_id pertenece a cajero o encargado


# ─── Actualización de usuario (CU-03) ────────────────────────────────────────

class UserUpdate(BaseModel):
    """
    Schema para actualizar datos de un usuario existente.
    Todos los campos son opcionales para admitir actualizaciones parciales (PATCH).
    """
    full_name: Optional[str] = None
    phone: Optional[str] = None
    role_id: Optional[int] = None
    branch_id: Optional[int] = None  # Permite reasignar sucursal
    is_active: Optional[bool] = None


# ─── Cambio de contraseña ─────────────────────────────────────────────────────

class PasswordChange(BaseModel):
    current_password: str
    new_password: str


# ─── Schema de respuesta (sin contraseña) ────────────────────────────────────

class UserInDBBase(UserBase):
    id: int
    role_id: Optional[int] = None
    branch_id: Optional[int] = None
    is_active: bool
    created_at: datetime
    role: Optional[RoleSchema] = None
    user_permissions: List[UserPermissionSchema] = []

    class Config:
        from_attributes = True


class User(UserInDBBase):
    """Schema de respuesta público (sin hashed_password)."""
    pass


class UserInDB(UserInDBBase):
    """Schema interno con contraseña hasheada (solo para uso interno)."""
    hashed_password: str


# ─── Schemas de Token JWT ─────────────────────────────────────────────────────

class Token(BaseModel):
    access_token: str
    token_type: str
    role: Optional[str] = None # Role name
    tenant_id: Optional[int] = None
    plan_code: Optional[str] = None


class TokenPayload(BaseModel):
    sub: Optional[str] = None   # user_id como string
    role: Optional[str] = None
    tenant_id: Optional[int] = None
    plan_code: Optional[str] = None
    plan_id: Optional[int] = None

